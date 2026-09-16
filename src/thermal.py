"""Thermal photos: where the crop runs hot, and how likely a pest is behind it.

Entirely optional. Most growers have no thermal camera, and everything else in
Dos Ojos works without one. When a flight does carry a radiometric thermal
band, it answers one question the colour camera cannot: which plants have shut
their stomata. A plant that stops transpiring stops cooling itself, so it sits a
degree or three above its neighbours long before it looks any different.

The catch, and the reason nothing here says "pest" on its own: a hot patch has
three common causes and thermal cannot tell them apart.

- The crop is thirsty. The whole field runs hot, evenly.
- The water never reached it. A high spot, the tail of a row, a blocked emitter.
- Something is eating it, or blocking it. Root rot, nematodes, borers, sucking
  insects, wilt: the roots or the stem stop delivering water to leaves that are
  otherwise fine.

So a warm patch is scored against what the rest of Dos Ojos already knows: the
ground model from ``terrain``, the per-unit verdicts from ``flag``, and the
water checkbook from the satellite half. A patch that is hot while the field has
water, sits on no high spot, and holds crop the colour camera already called
stressed, is the one worth walking out to. That score is a scorecard, not a
calibrated probability: we have no labelled pest data to fit one, so the number
is only ever a way of ranking patches, and every report carries the signs it was
built from so a grower can disagree with it.

Conditions matter as much as the camera. Fly within two hours of solar noon,
under a clear sky, in light wind, with the crop lit rather than shaded. A thin
cloud passing over mid-flight will paint a cold stripe across the mosaic that
looks nothing like a pest but wrecks the statistics all the same.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field as dc_field

import numpy as np
from shapely.geometry import Polygon, box
from shapely.geometry.base import BaseGeometry

from .chm import Surface, load_surface
from .terrain import where

log = logging.getLogger(__name__)

KELVIN_ZERO = 273.15

#: Canopy this tall and above is crop; below it the sensor is looking at soil,
#: which on a sunny afternoon runs 20 degrees hotter than any leaf and would
#: swamp every statistic here.
CANOPY_MIN_M = 0.30
#: A patch smaller than this is noise, a bird or a single plant, not something
#: to send anyone across a field for. Measured on the patch's footprint on the
#: ground, which is what a grower walks.
MIN_PATCH_M2 = 25.0
#: ...and never smaller than this share of the canopy, because noise scales with
#: the field. On a 45-acre field the temperature noise alone clumps into dozens
#: of 40 m2 blobs, every one of them meaningless; an absolute floor lets them all
#: through while a relative one does not. The larger of the two governs.
MIN_PATCH_SHARE = 0.002
#: Warm canopy within this distance is one patch. An orchard's canopy is
#: disconnected by construction - round crowns with bare ground between them -
#: so tracing connected warm pixels would break a warm block of twenty trees
#: into twenty patches of 15 m2 each and report none of them. Closing the mask
#: over about one tree spacing joins neighbours without inflating a lone tree,
#: since closing restores an isolated blob to its own size.
GROUP_GAP_M = 6.0
#: How far above the canopy median a pixel must sit, in robust standard
#: deviations of this field's own canopy temperature...
WARM_Z = 2.0
#: ...and in plain degrees, so a very even field does not report its own noise.
MIN_WARM_C = 1.0
#: Converts an interquartile range into a standard-deviation equivalent.
IQR_TO_SIGMA = 1.349
#: A canopy median outside this range means the numbers are not temperatures.
PLAUSIBLE_C = (-20.0, 70.0)
#: Share of the canopy that may run warm before this is a field-wide problem
#: (thirst, or a flight through cloud) rather than a set of patches.
MAX_WARM_SHARE = 0.35
#: The score is never allowed to sound certain in either direction.
CHANCE_FLOOR, CHANCE_CEILING = 0.10, 0.85


class ThermalError(RuntimeError):
    """Raised when a thermal raster cannot be read or is not a temperature."""


# --------------------------------------------------------------------------- #
# Reading a thermal raster
# --------------------------------------------------------------------------- #


def to_celsius(data: np.ndarray) -> tuple[np.ndarray, str]:
    """Degrees Celsius from whatever the camera wrote, and which unit that was.

    Radiometric thermal comes off drones in three shapes: Celsius already,
    Kelvin, or the hundredths of a Kelvin that FLIR-derived cameras store in a
    16-bit integer. They sit in ranges far enough apart to tell by eye.

    Raises:
        ThermalError: if no reading puts the canopy at a believable temperature,
            which is what a plain greyscale (non-radiometric) image looks like.
    """
    finite = data[np.isfinite(data)]
    if finite.size == 0:
        raise ThermalError("the thermal raster holds no values")
    middle = float(np.median(finite))
    if middle > 2_000:
        return data / 100.0 - KELVIN_ZERO, "centi-kelvin"
    if middle > 200:
        return data - KELVIN_ZERO, "kelvin"
    if PLAUSIBLE_C[0] <= middle <= PLAUSIBLE_C[1]:
        return data.astype(np.float64), "celsius"
    raise ThermalError(
        f"the middle value of this raster is {middle:.0f}, which is no temperature in "
        "Celsius, Kelvin or hundredths of a Kelvin. A picture that only looks like heat "
        "(a grey or orange JPEG) carries no temperatures at all; the camera has to be a "
        "radiometric one and the file has to keep its numbers."
    )


def load_thermal(path) -> tuple[Surface, str]:
    """A thermal orthophoto as degrees Celsius, with the unit it was stored in."""
    surface = load_surface(path)
    celsius, unit = to_celsius(surface.data)
    log.info("thermal %s read as %s, canopy and ground span %.1f to %.1f C",
             surface.path.name, unit, float(np.nanmin(celsius)), float(np.nanmax(celsius)))
    return Surface(celsius, surface.transform, surface.crs, surface.path), unit


def align(source: Surface, target: Surface) -> np.ndarray:
    """``source`` resampled onto ``target``'s grid, so the two can be compared.

    The thermal camera and the colour camera almost never share a resolution,
    and ODM writes each mosaic on its own grid.
    """
    from rasterio.warp import Resampling, reproject

    if source.data.shape == target.data.shape and source.transform == target.transform:
        return source.data
    out = np.full(target.data.shape, np.nan, dtype=np.float64)
    reproject(
        source=source.data, destination=out,
        src_transform=source.transform, src_crs=source.crs,
        dst_transform=target.transform, dst_crs=target.crs,
        src_nodata=np.nan, dst_nodata=np.nan, resampling=Resampling.bilinear,
    )
    return out


# --------------------------------------------------------------------------- #
# Warm patches
# --------------------------------------------------------------------------- #


@dataclass
class Patch:
    """One stretch of canopy running hotter than the rest of the field."""

    label: int
    #: Ground the patch spans: what a grower walks out to.
    area_m2: float
    #: Warm leaf area inside it, which in an orchard is a third of the footprint.
    canopy_m2: float
    mean_c: float
    peak_c: float
    #: Degrees above the field's canopy median, which is what actually matters.
    above_c: float
    z: float
    where: str
    compact: float             # 0 to 1: a blob is near 1, a row-long streak near 0
    #: Share of the flagged units inside it that the colour camera already
    #: called STRESSED or DEAD, and how many units that was.
    problem_share: float | None = None
    n_units: int = 0
    #: Whether the ground model puts a high or low spot under it.
    ground: str | None = None
    chance: float = 0.0
    signs: list[str] = dc_field(default_factory=list)
    geometry: BaseGeometry | None = dc_field(default=None, repr=False)

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload.pop("geometry")
        return payload


@dataclass
class ThermalReport:
    """Everything the thermal step found for one flight."""

    flight_id: str
    field_id: str | None
    unit: str
    cell_m: float
    canopy_m2: float
    canopy_median_c: float
    canopy_spread_c: float
    warm_share: float
    patches: list[Patch]
    notes: list[str]

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["patches"] = [p.to_dict() for p in self.patches]
        return payload


def canopy_mask(celsius: np.ndarray, canopy_m: np.ndarray,
                canopy_min_m: float = CANOPY_MIN_M) -> np.ndarray:
    """Pixels that are leaves, measured on both rasters at once.

    Everything downstream works on these and only these, the figure included:
    sunlit soil between the rows runs twenty degrees hotter than any leaf, so a
    picture that includes it flattens every difference that matters into one shade.
    """
    return np.isfinite(celsius) & np.isfinite(canopy_m) & (canopy_m >= canopy_min_m)


def robust_stats(values: np.ndarray) -> tuple[float, float]:
    """Median and a standard-deviation equivalent that a hot patch cannot drag."""
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        raise ThermalError("no canopy pixels to measure")
    median = float(np.median(finite))
    q25, q75 = np.percentile(finite, (25, 75))
    return median, float(q75 - q25) / IQR_TO_SIGMA


def find_patches(
    celsius: np.ndarray, canopy_m: np.ndarray, transform, *, cell_m: float,
    frame: BaseGeometry, canopy_min_m: float = CANOPY_MIN_M, warm_z: float = WARM_Z,
    min_warm_c: float = MIN_WARM_C, min_patch_m2: float = MIN_PATCH_M2,
    min_patch_share: float = MIN_PATCH_SHARE, group_gap_m: float = GROUP_GAP_M,
) -> tuple[list[Patch], dict]:
    """Trace connected stretches of canopy running hot, with the field's own stats.

    Only canopy is measured. Sunlit soil between the rows runs far hotter than
    any leaf, so a mask taken on temperature alone would trace the furrows.
    """
    from rasterio.features import shapes
    from scipy import ndimage

    canopy = canopy_mask(celsius, canopy_m, canopy_min_m)
    if not canopy.any():
        raise ThermalError(
            f"no canopy at least {canopy_min_m:g} m tall under the thermal mosaic; with the "
            "field bare there is nothing transpiring to measure"
        )
    values = np.where(canopy, celsius, np.nan)
    median, spread = robust_stats(values)
    if spread <= 0:
        raise ThermalError("every canopy pixel reads the same temperature; "
                           "the file is probably not radiometric")

    above = values - median
    warm = canopy & (above >= max(min_warm_c, warm_z * spread))
    stats = {
        "canopy_m2": float(canopy.sum()) * cell_m ** 2,
        "canopy_median_c": round(median, 2),
        "canopy_spread_c": round(spread, 2),
        "warm_share": float(warm.sum()) / float(canopy.sum()),
    }

    smallest = max(min_patch_m2, min_patch_share * stats["canopy_m2"])
    grouped = ndimage.binary_closing(warm, structure=_disc(0.5 * group_gap_m / cell_m))
    labels, count = ndimage.label(grouped)
    patches: list[Patch] = []
    for label in range(1, count + 1):
        mask = labels == label
        area = float(mask.sum()) * cell_m ** 2
        if area < smallest:
            continue
        leaves = mask & warm
        pieces = [Polygon(s["coordinates"][0], s["coordinates"][1:])
                  for s, v in shapes(mask.astype(np.uint8), mask=mask, transform=transform)
                  if v == 1]
        geometry = max(pieces, key=lambda g: g.area) if pieces else None
        # Temperatures come from the warm leaves only; the bare ground the
        # closing swept in between them is 20 C hotter and means nothing here.
        hot = above[leaves]
        patches.append(Patch(
            label=label, area_m2=round(area, 1),
            canopy_m2=round(float(leaves.sum()) * cell_m ** 2, 1),
            mean_c=round(median + float(np.nanmean(hot)), 2),
            peak_c=round(median + float(np.nanmax(hot)), 2),
            above_c=round(float(np.nanmean(hot)), 2),
            z=round(float(np.nanmean(hot)) / spread, 2),
            where=where(geometry.centroid, frame) if geometry is not None else "middle",
            compact=round(_compactness(geometry), 2) if geometry is not None else 0.0,
            geometry=geometry,
        ))
    patches.sort(key=lambda p: p.area_m2 * p.above_c, reverse=True)
    log.info("%d warm patch(es) of at least %.0f m2 over %.0f m2 of canopy, "
             "median %.1f C +/- %.1f", len(patches), smallest, stats["canopy_m2"],
             median, spread)
    return patches, stats


def _disc(radius_cells: float) -> np.ndarray:
    """A round structuring element, so closing joins neighbours in every direction."""
    r = max(1, int(round(radius_cells)))
    y, x = np.ogrid[-r:r + 1, -r:r + 1]
    return (x ** 2 + y ** 2) <= r ** 2


def _compactness(geometry: BaseGeometry) -> float:
    """1 for a circle, near 0 for a long thin streak along a row.

    Water problems run with the rows and the slope, so they come out as streaks;
    an infestation starts at a point and spreads outwards.
    """
    perimeter = geometry.length
    if perimeter <= 0:
        return 0.0
    return min(1.0, 4 * np.pi * geometry.area / perimeter ** 2)


# --------------------------------------------------------------------------- #
# How likely is it a pest
# --------------------------------------------------------------------------- #

#: Each sign that a warm patch is something living rather than the water or the
#: ground, and what it is worth. The weights are judgement, not a fit: there is
#: no labelled set of infested Valley fields to fit them to. They exist to rank
#: patches and to show a grower the reasoning, never to diagnose.
SIGNS: dict[str, tuple[float, str, str]] = {
    "hot": (2.0, "runs {above_c:.1f} C above the rest of the canopy",
            "a degree or two is ordinary variation; three is a plant that has stopped drinking"),
    "warm": (1.0, "runs {above_c:.1f} C above the rest of the canopy", ""),
    "faint": (-1.0, "is only {above_c:.1f} C above the rest of the canopy",
              "within what an ordinary field varies by on its own"),
    "big": (1.0, "covers {area_m2:.0f} m2", "large enough to be worth walking out to"),
    "compact": (1.0, "is a blob rather than a streak along the rows",
                "water problems follow the rows and the slope; an infestation spreads outwards"),
    "streak": (-1.0, "runs as a streak along the rows",
               "that is the shape a watering problem makes"),
    "flat_ground": (2.0, "sits on ground the laser found level",
                    "so the water was not simply missing it"),
    "high_ground": (-2.0, "sits on a high spot the water struggles to reach",
                    "the ground explains the heat without any pest"),
    "low_ground": (-1.0, "sits in a low spot where water stands",
                   "waterlogged roots also stop a plant drinking"),
    "field_watered": (2.0, "is hot while the field still has water",
                      "a thirsty field runs hot all over, not in patches"),
    "field_dry": (-2.0, "is hot on a field that is due water anyway",
                  "the whole crop is short, so this patch says little"),
    "crop_damaged": (2.0, "holds {problem_share:.0%} of its plants already flagged small or "
                     "pale, against {field_share:.0%} across the field",
                     "the colour camera can see the damage too"),
    "crop_fine": (-1.0, "holds plants the colour camera finds normal",
                  "hot but healthy-looking is more often water than pest"),
}


@dataclass(frozen=True)
class Evidence:
    """What the rest of Dos Ojos knows about this flight, for the scorecard.

    Every field is optional: with none of them a patch is judged on its own heat
    and shape alone, and says so.
    """

    #: Days of water the checkbook says the field has left; None if unknown.
    water_days: int | None = None
    #: Share of the whole field's units the colour camera flagged, for comparison.
    field_problem_share: float | None = None
    #: High and low spots from ``terrain``, as (kind, geometry).
    spots: tuple = ()


def score(patch: Patch, evidence: Evidence = Evidence()) -> Patch:
    """Fill in a patch's ``chance`` and the signs behind it.

    The number is a ranking, not a diagnosis: see this module's own docstring.
    """
    points, signs = 0.0, []

    def take(key: str, **values) -> None:
        nonlocal points
        weight, finding, because = SIGNS[key]
        points += weight
        line = finding.format(**{**patch.to_dict(), **values})
        signs.append(f"{line} - {because}" if because else line)

    if patch.above_c >= 3.0:
        take("hot")
    elif patch.above_c >= 2.0:
        take("warm")
    else:
        take("faint")
    if patch.area_m2 >= 200:
        take("big")
    if patch.compact >= 0.35:
        take("compact")
    elif patch.compact <= 0.15:
        take("streak")

    if patch.ground == "high":
        take("high_ground")
    elif patch.ground == "low":
        take("low_ground")
    elif evidence.spots:
        take("flat_ground")

    if evidence.water_days is not None:
        take("field_watered" if evidence.water_days >= 3 else "field_dry")

    field_share = evidence.field_problem_share
    if patch.problem_share is not None and field_share is not None:
        if patch.problem_share > max(0.10, 1.5 * field_share):
            take("crop_damaged", field_share=field_share)
        elif patch.problem_share <= field_share:
            take("crop_fine")

    patch.signs = signs
    patch.chance = round(_curve(points), 2)
    return patch


def _curve(points: float) -> float:
    """Points to a share, flattened at both ends so it never sounds certain.

    Three points is an even chance: that is roughly "hot, compact, and nothing
    else explains it". Everything below sits under a half, everything above
    climbs towards, but never reaches, the ceiling.
    """
    raw = 1.0 / (1.0 + np.exp(-(points - 3.0) / 1.6))
    return float(np.clip(raw, CHANCE_FLOOR, CHANCE_CEILING))


def place_on_ground(patches: list[Patch], spots) -> None:
    """Mark each patch with the kind of ground under it, from ``terrain``'s spots.

    ``spots`` is any sequence of objects carrying ``kind`` and ``geometry``.
    """
    for patch in patches:
        if patch.geometry is None:
            continue
        for spot in spots:
            geometry = getattr(spot, "geometry", None)
            if geometry is not None and geometry.intersects(patch.geometry):
                overlap = geometry.intersection(patch.geometry).area
                if overlap >= 0.3 * patch.geometry.area:
                    patch.ground = getattr(spot, "kind", None)
                    break


def place_on_units(patches: list[Patch], units) -> None:
    """Count how much of each patch's crop the colour camera already flagged.

    ``units`` is the GeoDataFrame written by ``flag``; a patch with too few
    units under it is left unjudged rather than scored on one or two plants.
    """
    if units is None or units.empty or "flag" not in units:
        return
    judged = units[~units["flag"].isin(("EDGE", "NO_DATA"))]
    for patch in patches:
        if patch.geometry is None or judged.empty:
            continue
        inside = judged[judged.geometry.intersects(patch.geometry)]
        patch.n_units = int(len(inside))
        if patch.n_units >= 4:
            hit = inside["flag"].isin(("STRESSED", "DEAD", "MISSING")).sum()
            patch.problem_share = round(float(hit) / patch.n_units, 3)


def frame_of(surface: Surface, outline: BaseGeometry | None) -> BaseGeometry:
    """The extent place names are measured against: the field, or the mosaic."""
    if outline is not None and not outline.is_empty:
        return outline
    rows, columns = surface.data.shape
    west, north = surface.transform * (0, 0)
    east, south = surface.transform * (columns, rows)
    return box(min(west, east), min(north, south), max(west, east), max(north, south))


def build_report(
    flight_id: str, field_id: str | None, *, unit: str, cell_m: float, patches: list[Patch],
    stats: dict, evidence: Evidence = Evidence(), notes: list[str] | None = None,
) -> ThermalReport:
    """Score every patch and gather the flight's thermal findings."""
    notes = list(notes or [])
    if stats["warm_share"] > MAX_WARM_SHARE:
        notes.append(
            f"{stats['warm_share']:.0%} of the canopy is running hot, which is too much of the "
            "field to be patches. Either the crop is short of water everywhere, or the flight "
            "passed through cloud or wind. Treat the patches below as places to look, nothing more."
        )
    if evidence.water_days is None:
        notes.append("no water checkbook was given for this field, so nobody could tell a "
                     "thirsty field from a bitten one; run the satellite half and pass "
                     "--water-days.")
    for patch in patches:
        score(patch, evidence)
    return ThermalReport(
        flight_id=flight_id, field_id=field_id, unit=unit, cell_m=round(cell_m, 3),
        canopy_m2=round(stats["canopy_m2"], 1), canopy_median_c=stats["canopy_median_c"],
        canopy_spread_c=stats["canopy_spread_c"], warm_share=round(stats["warm_share"], 4),
        patches=patches, notes=notes,
    )
