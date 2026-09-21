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
import pandas as pd
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

#: The strongest periodicity in a field is not always the rows. Anything that
#: repeats at a multiple of the row spacing puts power at its own harmonics: on
#: a real sorghum trial whose every plot had the same two rows cut, the pattern
#: repeated every plot width (9.14 m) and its fifth harmonic, 1.8 m, outshone
#: the 0.76 m rows. Such patterns do not repeat from one row to the next, so the
#: rows are confirmed on short strips along them, where the across-row profile
#: must repeat at the chosen spacing. Strips are short so that a small error in
#: the direction cannot smear one row into the next within a strip.
STRIP_LENGTH_M = 5.0

#: A lag must correlate at least this well, and rise at least this far above the
#: dip before it, to count as the canopy repeating from row to row.
MIN_REPEAT_CORRELATION = 0.10
MIN_REPEAT_PROMINENCE = 0.10

#: The spectral peak and the strip repeat agree when within this share.
REPEAT_TOLERANCE = 0.08

#: A peak the strip repeat points to must carry at least this share of the
#: strongest peak's power; weaker, and it is a coincidence in empty spectrum.
MIN_RELATIVE_PEAK = 0.02

#: Rows are located tile by tile rather than once for the field. A direction a
#: quarter of a degree off walks a row half a row sideways over 100 m, and a
#: planter's passes rarely meet at exactly the row spacing; measured per tile of
#: this many metres along and this many rows across, either moves a row by a
#: centimetre or two within its tile.
PHASE_TILE_ALONG_M = 5.0
PHASE_TILE_ROWS = 8

#: Tiles whose ridges are this incoherent (a gap, a road, bare soil) carry no
#: position of their own and take their neighbours'.
MIN_TILE_COHERENCE = 0.05

#: Width, in rows, of the running median removed before tiles are measured.
#: Wide enough to sit level across a row and its furrow, narrow enough to
#: follow a plot edge or a two-row gap.
STEP_WINDOW_ROWS = 2.5

#: Row tracking moves segments only when the rows really wander: when the
#: fitted offsets depart from a straight comb by more than this share of the
#: spacing somewhere in the field. Below it a straight comb is as good, and
#: measuring it field-wide averages away every tile's noise.
MIN_TRACKED_DRIFT = 0.15

#: Pixels used for row analysis; larger rasters are thinned along the rows only.
MAX_FRAME_PIXELS = 6_000_000

#: Row spacings growers actually plant, in metres. A detected spacing outside
#: its crop's range is more likely a pattern laid over the rows than the rows:
#: on a real July sorghum trial the detector read 1.79 m against a planted
#: 0.76 m, with a strength that looked confident.
TYPICAL_SPACING_M = {
    "sorghum": (0.35, 1.05),
    "corn": (0.35, 1.05),
    "maize": (0.35, 1.05),
    "cotton": (0.50, 1.05),
    "soy": (0.18, 0.80),
    "cane": (1.20, 2.20),
}


def spacing_warning(spacing_m: float, crop: str | None) -> str | None:
    """A warning when a detected spacing is implausible for the crop, else None."""
    text = (crop or "").lower()
    for name, (low, high) in TYPICAL_SPACING_M.items():
        if name in text:
            if low <= spacing_m <= high:
                return None
            return (
                f"a {spacing_m:.2f} m row spacing is unusual for {name}, normally planted "
                f"{low:.2f}-{high:.2f} m apart, so the detector has probably locked onto a "
                "pattern laid over the rows. If you know the planter spacing, pass "
                "--spacing (30 in = 0.762) or record it with 'register --row-spacing'."
            )
    return None


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
    known_spacing_m: float | None = None,
) -> RowGeometry:
    """Recover row spacing and direction from the canopy model's periodicity.

    Planted rows make the canopy periodic in the direction across them, which is
    a single bright peak in the two-dimensional power spectrum. The peak's angle
    gives the across-row direction and its frequency gives the spacing, both
    without needing to trace any individual row.

    A peak always exists, so the returned strength is what says whether to
    believe it. See :data:`MIN_ROW_STRENGTH` for how it is measured and why.

    With ``known_spacing_m`` (a grower knows their planter) only the direction
    is searched, near that spacing. This matters once the canopy closes: on a
    real July sorghum scan the 0.76 m rows stood 1.7 times above the spectrum
    around them while the plots' own pattern stood at 3 to 4.

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

    # Strength is always judged against the whole searched band, so a known
    # spacing narrows where the peak is looked for but not what it is compared to.
    in_band = spectrum[band]
    reference = float(np.percentile(in_band, 99))
    if known_spacing_m is not None:
        near = np.abs(radius - 1.0 / known_spacing_m) <= REPEAT_TOLERANCE / known_spacing_m
        band = band & near
        if not band.any():
            raise DetectionError(
                f"a {known_spacing_m} m spacing does not fit this raster's resolution and extent"
            )
    peak_index = np.unravel_index(np.argmax(np.where(band, spectrum, -np.inf)), spectrum.shape)
    spacing, direction_deg = _peak_geometry(grid_x, grid_y, peak_index)

    # Confirm on short strips that the canopy repeats row to row at this spacing,
    # and move to the peak it does repeat at if not. Patterns laid over the rows
    # share their direction, so only the spacing is in question.
    if known_spacing_m is None:
        found = _repeat_spacing(chm, resolution_m, direction_deg, min_spacing_m, max_spacing_m)
        if found is not None and abs(spacing - found[0]) > REPEAT_TOLERANCE * found[0]:
            near = _peak_near(spectrum, grid_x, grid_y, band, found[0], direction_deg)
            # Only a peak carrying real power is worth moving to.
            if near is not None and spectrum[near] >= MIN_RELATIVE_PEAK * spectrum[peak_index]:
                log.info(
                    "the strongest periodicity (%.2f m) is a pattern laid over the "
                    "rows, not the rows; the canopy repeats every %.2f m",
                    spacing, found[0],
                )
                peak_index = near
                spacing, direction_deg = _peak_geometry(grid_x, grid_y, peak_index)

    peak_value = float(spectrum[peak_index])
    strength = peak_value / reference if reference > 0 else 0.0
    if known_spacing_m is not None:
        spacing = known_spacing_m

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


def _peak_geometry(grid_x, grid_y, index) -> tuple[float, float]:
    """Spacing and row direction for one spectral peak."""
    peak_x, peak_y = float(grid_x[index]), float(grid_y[index])
    across_deg = math.degrees(math.atan2(peak_y, peak_x)) % 180.0
    return 1.0 / math.hypot(peak_x, peak_y), (across_deg + 90.0) % 180.0


def _peak_near(spectrum, grid_x, grid_y, band, spacing_m: float, direction_deg: float):
    """The strongest spectral peak near a spacing and row direction, or None."""
    radius = np.hypot(grid_x, grid_y)
    target = 1.0 / spacing_m
    across = np.degrees(np.arctan2(grid_y, grid_x)) % 180.0
    row_dir = (across + 90.0) % 180.0
    diff = np.abs(row_dir - direction_deg) % 180.0
    near = (
        band
        & (np.abs(radius - target) <= REPEAT_TOLERANCE * target)
        & (np.minimum(diff, 180.0 - diff) <= 10.0)
    )
    if not near.any():
        return None
    return np.unravel_index(np.argmax(np.where(near, spectrum, -np.inf)), spectrum.shape)


def _pixel_frame(chm: np.ndarray, resolution_m: float, direction_deg: float):
    """Across-row and along-row position of valid pixel centres, and their heights.

    Coordinates are metres in the grid frame (x right, y down), matching the
    row segments. Large rasters are thinned along the rows only: across them,
    where the analysis happens, every pixel is kept.
    """
    theta = math.radians(direction_deg)
    rows, cols = chm.shape
    n_valid = int(np.isfinite(chm).sum())
    stride = max(1, math.ceil(n_valid / MAX_FRAME_PIXELS))
    if abs(math.cos(theta)) >= abs(math.sin(theta)):
        r_idx, c_idx = np.arange(rows), np.arange(0, cols, stride)
    else:
        r_idx, c_idx = np.arange(0, rows, stride), np.arange(cols)
    sub = chm[np.ix_(r_idx, c_idx)]
    valid = np.isfinite(sub)
    rr, cc = np.nonzero(valid)
    y = (r_idx[rr] + 0.5) * resolution_m
    x = (c_idx[cc] + 0.5) * resolution_m
    across = -x * math.sin(theta) + y * math.cos(theta)
    along = x * math.cos(theta) + y * math.sin(theta)
    return across, along, sub[valid].astype(np.float64)


def _repeat_spacing(
    chm: np.ndarray, resolution_m: float, direction_deg: float,
    min_spacing_m: float, max_spacing_m: float,
) -> tuple[float, float] | None:
    """The smallest lag at which the canopy repeats across rows, and how well.

    Profiles across the rows are averaged over short strips along them, their
    slow variation removed, and their autocorrelation pooled over all strips.
    Rows give a clear peak at one spacing; a pattern repeating every few rows,
    such as the same rows cut in every plot, does not.

    Returns:
        ``(spacing_m, correlation)`` of the first convincing repeat, or None.
    """
    from scipy.ndimage import gaussian_filter1d

    across, along, heights = _pixel_frame(chm, resolution_m, direction_deg)
    if heights.size < 100:
        return None
    bin_m = resolution_m
    ub = ((across - across.min()) / bin_m).astype(np.int64)
    vb = ((along - along.min()) / STRIP_LENGTH_M).astype(np.int64)
    n_u, n_v = int(ub.max()) + 1, int(vb.max()) + 1
    key = vb * n_u + ub
    sums = np.bincount(key, weights=heights, minlength=n_u * n_v).reshape(n_v, n_u)
    counts = np.bincount(key, minlength=n_u * n_v).reshape(n_v, n_u)
    mask = (counts > 0).astype(np.float64)
    profile = np.where(counts > 0, sums / np.maximum(counts, 1), 0.0)

    # Remove what varies slower than the widest spacing searched: plot-to-plot
    # height differences would otherwise dominate every lag.
    sigma = max(1.0, max_spacing_m / 2.0 / bin_m)
    smooth = gaussian_filter1d(profile * mask, sigma, axis=1, mode="constant")
    weight = gaussian_filter1d(mask, sigma, axis=1, mode="constant")
    detail = np.where(mask > 0, profile - smooth / np.maximum(weight, 1e-9), 0.0)

    # A canopy that does not vary across this direction has nothing to repeat;
    # normalising its rounding noise would invent a perfect correlation.
    if float((detail**2).sum()) <= 1e-9 * max(float((profile**2).sum()), 1e-12):
        return None

    n_fft = 2 * n_u
    spec = np.fft.rfft(detail, n=n_fft, axis=1)
    spec_mask = np.fft.rfft(mask, n=n_fft, axis=1)
    products = np.fft.irfft((spec * spec.conj()).sum(axis=0), n=n_fft)[:n_u]
    pairs = np.fft.irfft((spec_mask * spec_mask.conj()).sum(axis=0), n=n_fft)[:n_u]
    if pairs[0] <= 0 or products[0] <= 0:
        return None
    acf = (products / np.maximum(pairs, 1.0)) / (products[0] / pairs[0])

    lo = max(1, math.ceil(min_spacing_m / bin_m))
    hi = min(n_u - 2, math.floor(max_spacing_m / bin_m))
    # Lags backed by too few pixel pairs are noise.
    trusted = np.nonzero(pairs < 0.25 * pairs[0])[0]
    if trusted.size:
        hi = min(hi, int(trusted[0]) - 1)
    for i in range(lo, hi + 1):
        value = acf[i]
        if value < MIN_REPEAT_CORRELATION or not (value > acf[i - 1] and value >= acf[i + 1]):
            continue
        if value - float(acf[1:i].min()) < MIN_REPEAT_PROMINENCE:
            continue
        a, b, c = acf[i - 1], acf[i], acf[i + 1]
        curvature = a - 2.0 * b + c
        offset = 0.5 * (a - c) / curvature if curvature != 0 else 0.0
        return (i + float(np.clip(offset, -0.5, 0.5))) * bin_m, float(value)
    return None


@dataclass(frozen=True)
class PhaseField:
    """Where the ridges sit, tile by tile, as an unwrapped offset across the rows."""

    across0: float
    along0: float
    tile_across: float
    tile_along: float
    offset: np.ndarray        # (n_along, n_across) ridge offset in metres, unwrapped

    def at(self, across: np.ndarray, along: np.ndarray) -> np.ndarray:
        """Ridge offset at the given positions, bilinear between tile centres."""
        from scipy.ndimage import map_coordinates

        fi = (np.asarray(along) - self.along0) / self.tile_along - 0.5
        fj = (np.asarray(across) - self.across0) / self.tile_across - 0.5
        return map_coordinates(self.offset, [fi, fj], order=1, mode="nearest")


def local_phase_field(
    chm: np.ndarray, resolution_m: float, geometry: RowGeometry
) -> PhaseField | None:
    """Measure where the rows sit across the field, so segments follow the rows.

    Each tile's ridge offset comes from the phase of its first Fourier
    coefficient at the row spacing, unwrapped so a row keeps its index from one
    end of the field to the other. Single tiles are not trusted, though: once
    the canopy closes their phases are noisy, and on a real closed-canopy trial
    following them tile by tile put segments 11 cm off rows that one straight
    comb fit to within 1 cm. What is kept is what real rows do: a plane, for a
    slightly wrong direction or spacing drifting steadily across the field, and
    one sideways offset per band of rows, for planter passes that do not meet at
    exactly the row spacing, taken as the median over the whole length of the
    rows. Returns None when no tile shows rows clearly enough, or when the rows
    wander less than :data:`MIN_TRACKED_DRIFT` of a row, where one straight comb
    measured over the whole field is the better estimate.
    """
    from scipy.ndimage import distance_transform_edt, median_filter

    spacing = geometry.spacing_m
    across, along, heights = _pixel_frame(chm, resolution_m, geometry.direction_deg)
    if heights.size < 100:
        return None
    tile_across = PHASE_TILE_ROWS * spacing
    tile_along = PHASE_TILE_ALONG_M
    across0, along0 = float(across.min()), float(along.min())

    # Across-row profiles per strip along the rows, one bin per pixel width.
    bins = ((across - across0) / resolution_m).astype(np.int64)
    strips = ((along - along0) // tile_along).astype(np.int64)
    n_bins, n_v = int(bins.max()) + 1, int(strips.max()) + 1
    key = strips * n_bins + bins
    counts = np.bincount(key, minlength=n_v * n_bins).reshape(n_v, n_bins)
    sums = np.bincount(key, weights=heights, minlength=n_v * n_bins).reshape(n_v, n_bins)
    valid = counts > 0
    profile = np.where(valid, sums / np.maximum(counts, 1), np.nan)
    if (~valid).any():
        _, (ri, ci) = distance_transform_edt(~valid, return_indices=True)
        filled = profile[ri, ci]
    else:
        filled = profile

    # A running median follows a step exactly, so plot edges, alleys and cut
    # rows leave nothing behind; a linear filter would leave them a component
    # at the row frequency, which on the real trial moved tiles by centimetres.
    window = max(3, int(round(STEP_WINDOW_ROWS * spacing / resolution_m)) | 1)
    detail = np.where(valid, filled - median_filter(filled, size=(1, window), mode="nearest"), 0.0)

    centres = across0 + (np.arange(n_bins) + 0.5) * resolution_m
    phasor = np.exp(2j * np.pi * centres / spacing)
    column = ((centres - across0) // tile_across).astype(np.int64)
    n_u = int(column.max()) + 1
    weighted = counts * detail
    z = np.zeros((n_v, n_u), dtype=complex)
    magnitude = np.zeros((n_v, n_u))
    pixels = np.zeros((n_v, n_u))
    for j in range(n_u):
        cols = column == j
        z[:, j] = (weighted[:, cols] * phasor[cols]).sum(axis=1)
        magnitude[:, j] = np.abs(weighted[:, cols]).sum(axis=1)
        pixels[:, j] = counts[:, cols].sum(axis=1)
    coherence = np.abs(z) / np.maximum(magnitude, 1e-12)

    min_pixels = 0.25 * tile_across * tile_along / resolution_m**2
    good = (pixels >= min_pixels) & (coherence >= MIN_TILE_COHERENCE)
    if not good.any():
        return None
    phase = np.where(good, np.angle(z), np.nan)
    weight = np.where(good, coherence * pixels, 0.0)

    holes = ~np.isfinite(phase)
    if holes.any():
        _, (ri, ci) = distance_transform_edt(holes, return_indices=True)
        phase = phase[ri, ci]
    phase = np.unwrap(phase, axis=1)
    for j in range(1, n_v):
        step = np.median(phase[j] - phase[j - 1])
        phase[j] -= 2.0 * np.pi * np.round(step / (2.0 * np.pi))

    offset = _rows_model(phase * spacing / (2.0 * np.pi), weight)
    # Compare with where the straight comb would put the rows, not with a
    # constant: a spacing a percent off drifts the comb even over straight rows.
    apart = np.mod(offset - geometry.phase_m + spacing / 2.0, spacing) - spacing / 2.0
    drift = float(np.percentile(np.abs(apart), 90))
    if drift < MIN_TRACKED_DRIFT * spacing:
        log.info("rows sit within %.0f cm of one straight comb; using it for the whole field",
                 drift * 100)
        return None
    return PhaseField(
        across0=across0, along0=along0, tile_across=tile_across, tile_along=tile_along,
        offset=offset,
    )


def _rows_model(offset: np.ndarray, weight: np.ndarray) -> np.ndarray:
    """A plane plus one median residual per band of rows, fitted robustly.

    ``offset`` is (along, across) tiles of unwrapped ridge offset in metres and
    ``weight`` how far to trust each tile; tiles with no weight only fill gaps.
    """
    n_v, n_u = offset.shape
    vv, uu = np.mgrid[0:n_v, 0:n_u].astype(np.float64)
    design = np.column_stack([np.ones(offset.size), uu.ravel(), vv.ravel()])
    target = offset.ravel()
    w = weight.ravel().copy()
    if w.sum() <= 0:
        return offset
    coefficients = np.zeros(3)
    for _ in range(3):
        root = np.sqrt(w)
        coefficients, *_ = np.linalg.lstsq(design * root[:, None], target * root, rcond=None)
        residual = target - design @ coefficients
        spread = 1.4826 * np.median(np.abs(residual[w > 0])) + 1e-6
        # Down-weight tiles far from the plane, so a wrong unwrap cannot tilt it.
        w = weight.ravel() * (np.abs(residual) <= 2.5 * spread)
    plane = (design @ coefficients).reshape(n_v, n_u)

    residual = offset - plane
    bands = np.zeros(n_u)
    for j in range(n_u):
        trusted = weight[:, j] > 0
        if trusted.sum() >= 3:
            bands[j] = float(np.median(residual[trusted, j]))
    return plane + bands[None, :]


def _across_coordinates(shape: tuple[int, int], resolution_m: float, across_deg: float):
    """Distance of every pixel centre along the across-row axis, in metres."""
    rows, cols = shape
    y, x = np.mgrid[0:rows, 0:cols].astype(np.float64) + 0.5
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
    track_rows: bool = True,
) -> list[tuple[int, int, Polygon]]:
    """Cut the field into row-aligned rectangles, in grid coordinates.

    Returns ``(row_index, segment_index, polygon)`` with polygons in pixel
    units; the caller converts them to world coordinates using the raster's
    transform. With ``track_rows`` each segment is centred on the rows as
    measured in its own tile (see :func:`local_phase_field`), not on one
    straight line per row laid across the whole field.
    """
    rows, cols = chm.shape
    spacing = geometry.spacing_m
    width_m = width_m if width_m is not None else spacing
    extent = box(0, 0, cols * resolution_m, rows * resolution_m)

    theta = math.radians(geometry.direction_deg)
    along = np.array([math.cos(theta), math.sin(theta)])
    across = np.array([-math.sin(theta), math.cos(theta)])

    corners = np.array(extent.exterior.coords)
    across_range = corners @ across
    along_range = corners @ along

    field = local_phase_field(chm, resolution_m, geometry) if track_rows else None
    if field is None:
        low = high = geometry.phase_m
    else:
        low, high = float(field.offset.min()), float(field.offset.max())
        log.info("rows tracked over %d x %d tiles; ridge offset varies by %.2f m",
                 field.offset.shape[0], field.offset.shape[1], high - low)

    first = math.floor((across_range.min() - high) / spacing)
    last = math.ceil((across_range.max() - low) / spacing)
    ks = np.arange(first, last + 1)
    n_segments = max(1, int((along_range.max() - along_range.min()) // segment_m))

    segments: list[tuple[int, int, Polygon]] = []
    for segment_index in range(n_segments):
        start = along_range.min() + segment_index * segment_m
        centres = geometry.phase_m + ks * spacing
        if field is not None:
            middle = np.full(ks.shape, start + segment_m / 2.0)
            centres = field.at(centres, middle) + ks * spacing
            # The offset varies slowly across the rows, so one more pass settles it.
            centres = field.at(centres, middle) + ks * spacing
        for row_index, centre in zip(ks, centres):
            polygon = _segment_polygon(float(centre), start, segment_m, width_m, along, across)
            clipped = polygon.intersection(extent)
            if clipped.is_empty or clipped.area < (segment_m * width_m * 0.2):
                continue
            if clipped.geom_type != "Polygon":
                continue
            segments.append((int(row_index - first), segment_index, clipped))

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


#: Pieces of a unit smaller than this share of it are dropped when splitting by
#: block; a sliver across a block boundary measures the boundary, not the crop.
MIN_BLOCK_PIECE_FRACTION = 0.10


def assign_blocks(
    frame: gpd.GeoDataFrame, blocks: gpd.GeoDataFrame, *, label_column: str | None = None,
    min_fraction: float = MIN_BLOCK_PIECE_FRACTION,
) -> gpd.GeoDataFrame:
    """Cut units at block boundaries and label each piece with its block.

    A block is any part of a field that should be judged on its own: a variety,
    a planting date, a ratoon age, or one plot of a trial. Units falling outside
    every block, such as alleys between plots, are dropped. A unit crossing a
    boundary is split, and the pieces keep their unit id with the block appended
    so ids stay unique and stable.

    Raises:
        DetectionError: if the blocks layer is empty or no unit touches a block.
    """
    if blocks.empty:
        raise DetectionError("the blocks layer has no polygons")
    if label_column is None:
        candidates = [c for c in blocks.columns if c != blocks.geometry.name]
        label_column = candidates[0] if candidates else None
    elif label_column not in blocks.columns:
        raise DetectionError(
            f"blocks have no column {label_column!r}; available: "
            + ", ".join(c for c in blocks.columns if c != blocks.geometry.name)
        )

    zones = blocks.to_crs(frame.crs) if blocks.crs else blocks.set_crs(frame.crs)
    labels = (zones[label_column].astype(str) if label_column
              else pd.Series([f"b{i:03d}" for i in range(len(zones))], index=zones.index))
    zones = gpd.GeoDataFrame({"block": labels.values}, geometry=zones.geometry.values,
                             crs=frame.crs)

    original = frame.assign(_area=frame.geometry.area)
    pieces = gpd.overlay(original, zones, how="intersection", keep_geom_type=True)
    if pieces.empty:
        raise DetectionError("no detected unit overlaps any block; check the blocks file")
    pieces = pieces[pieces.geometry.area >= min_fraction * pieces["_area"]].copy()
    split = pieces["unit_id"].duplicated(keep=False)
    pieces.loc[split, "unit_id"] = pieces.loc[split, "unit_id"] + "@" + pieces.loc[split, "block"]
    pieces = pieces.drop(columns="_area").sort_values("unit_id").reset_index(drop=True)
    pieces.attrs.update(frame.attrs)
    log.info("kept %d piece(s) of %d unit(s) inside %d block(s); %d unit(s) split",
             len(pieces), len(frame), zones["block"].nunique(), int(split.sum()))
    return pieces
