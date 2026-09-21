"""Real fields of a chosen crop, from USDA's public Cropland Data Layer.

The Cropland Data Layer (CDL) is USDA NASS's yearly map of what grew on every
patch of US farmland, classified from satellite images and checked against
farm surveys. It is public domain, and from 2024 it is made at 10 m. Here it
answers "show me a cotton field, a cane field and a grove in the Valley" with
real fields of those crops in a named year, so the pipeline can be shown on
many crops without anyone's records.

A field is traced as a block of one crop, then shrunk inward so the pixels on
its edge, which mix in the neighbour and the road, never count. What comes out
is an outline inside the field, not its surveyed boundary.

Two fields of the same crop with a farm road between them trace as one block,
since the road is narrower than a 30 m cell. Where the map lost only a stretch of
the road, the block has a waist, and an opening wider than the waist cuts it: the
larger field is kept. Where it lost all of it, the maps of earlier years give the
two away, since the halves rarely grew the same crop every year. So an annual
crop's field is kept only if one crop covered it in each earlier year too.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

import numpy as np
import rasterio
import requests
from pyproj import Transformer
from rasterio.features import geometry_mask, shapes
from shapely.geometry import box, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform

from .config import SQ_M_PER_ACRE
from .fields import utm_epsg_for

log = logging.getLogger(__name__)

SERVICE = "https://pdi.scinet.usda.gov/image/rest/services/CDL_WM/ImageServer"
SOURCE = "USDA NASS Cropland Data Layer"
#: The service refuses larger images; wider areas would need tiling.
MAX_PIXELS = 4000
CELL_M = 30.0

#: CDL class values for each crop the water checkbook models.
CROP_CODES: dict[str, tuple[int, ...]] = {
    "corn": (1,), "cotton": (2,), "sorghum": (4,), "soybean": (5,),
    "sugarcane": (45,), "citrus": (72, 212), "onion": (49,),
}
#: How a crop is written in fields.geojson, so the checkbook picks its model.
CROP_LABELS = {"corn": "corn", "cotton": "cotton", "sorghum": "grain sorghum",
               "soybean": "soybean", "sugarcane": "sugarcane", "citrus": "citrus",
               "onion": "onions"}

#: Land resting between crops, which the map calls fallow, hay, grass or scrub from
#: year to year: one class when judging a field's history.
IDLE_CODES = frozenset({37, 61, 131, 152, 176})
#: Crops that stay for years, and that the map mistakes for grass in some of them,
#: so their history says nothing about where one field ends.
PERENNIAL = frozenset({"citrus", "sugarcane"})
#: Share of an outline under one crop, in every earlier year, for it to be one field.
MIN_HISTORY_PURITY = 0.75
#: Waists narrower than twice this are cut, and corners rounded to it.
NECK_M = 80.0

#: Towns to name a field by, so "Cotton near La Feria" says where to look.
RGV_TOWNS: dict[str, tuple[float, float]] = {
    "Mission": (-98.325, 26.216), "McAllen": (-98.230, 26.203), "Edinburg": (-98.163, 26.302),
    "Pharr": (-98.183, 26.195), "San Juan": (-98.155, 26.189), "Alamo": (-98.123, 26.184),
    "Donna": (-98.052, 26.170), "Weslaco": (-97.991, 26.159), "Mercedes": (-97.914, 26.150),
    "La Feria": (-97.824, 26.159), "Harlingen": (-97.696, 26.191), "San Benito": (-97.631, 26.133),
    "Elsa": (-97.993, 26.293), "Edcouch": (-97.961, 26.294), "La Villa": (-97.925, 26.299),
    "Monte Alto": (-97.972, 26.373), "Hargill": (-98.001, 26.442), "Raymondville": (-97.783, 26.481),
    "Lyford": (-97.790, 26.412), "Santa Rosa": (-97.826, 26.257), "Combes": (-97.724, 26.245),
    "Primera": (-97.759, 26.224), "Rio Hondo": (-97.582, 26.235), "Progreso": (-97.957, 26.092),
    "Hidalgo": (-98.263, 26.100), "Los Fresnos": (-97.476, 26.072), "Linn": (-98.107, 26.563),
    "La Joya": (-98.481, 26.247), "Sullivan City": (-98.563, 26.277),
}


class CropMapError(RuntimeError):
    """Raised when the crop map cannot be read or holds nothing usable."""


@dataclass(frozen=True)
class Candidate:
    """One field traced from the crop map, in lon/lat."""

    crop: str
    geometry: BaseGeometry
    acres: float
    purity: float          #: share of the outline's pixels that are this crop
    squareness: float      #: area over its tightest rectangle; fields are near 1
    town: str
    distance_km: float

    @property
    def score(self) -> float:
        """Pure, rectangular, and of an everyday size come first."""
        size = math.exp(-((math.log(self.acres) - math.log(50.0)) ** 2) / 2)
        return self.purity * 2 + self.squareness + size


# --------------------------------------------------------------------------- #
# The map
# --------------------------------------------------------------------------- #


def _cache_name(bbox: Sequence[float], year: int) -> str:
    digest = hashlib.sha1(",".join(f"{v:.4f}" for v in bbox).encode()).hexdigest()[:8]
    return f"cdl_{year}_{digest}.tif"


def fetch(bbox: Sequence[float], year: int, folder: Path, *, cell_m: float = CELL_M,
          get: Callable[..., requests.Response] | None = None) -> Path:
    """The crop map of ``year`` over a lon/lat box, as a GeoTIFF in UTM; cached.

    Raises:
        CropMapError: if the box is too large for one image, or the service fails.
    """
    get = get or requests.get
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / _cache_name(bbox, year)
    classes = folder / f"cdl_{year}_classes.json"
    if path.exists() and classes.exists():
        return path
    west, south, east, north = bbox
    epsg = utm_epsg_for(box(west, south, east, north))
    to_utm = Transformer.from_crs(4326, epsg, always_xy=True)
    xs, ys = zip(*[to_utm.transform(x, y) for x, y in ((west, south), (east, south),
                                                        (east, north), (west, north))])
    width, height = (max(xs) - min(xs)) / cell_m, (max(ys) - min(ys)) / cell_m
    if max(width, height) > MAX_PIXELS:
        raise CropMapError(f"a {width:.0f} x {height:.0f} pixel map is more than the service "
                           f"gives in one piece ({MAX_PIXELS}); choose a smaller box")
    params = {
        "bbox": f"{min(xs)},{min(ys)},{max(xs)},{max(ys)}", "bboxSR": epsg, "imageSR": epsg,
        "size": f"{round(width)},{round(height)}", "format": "tiff", "pixelType": "U8",
        "interpolation": "RSP_NearestNeighbor", "noData": 0,
        "mosaicRule": json.dumps({"where": f"Year = {year}"}), "f": "json",
    }
    try:
        answer = get(f"{SERVICE}/exportImage", params=params, timeout=120).json()
        if "href" not in answer:
            raise CropMapError(f"the USDA crop map service said: {answer.get('error', answer)}")
        image = get(answer["href"], timeout=300)
        image.raise_for_status()
        table = get(f"{SERVICE}/rasterAttributeTable", params={"f": "json"}, timeout=60).json()
    except (requests.RequestException, ValueError) as exc:
        raise CropMapError(f"could not reach the USDA crop map service: {exc}") from exc
    path.write_bytes(image.content)
    with rasterio.open(path) as dataset:
        if dataset.crs is None:
            path.unlink()
            raise CropMapError("the crop map came back without a coordinate system")
    rows = {str(f["attributes"]["Value"]): {k: f["attributes"][k] for k in
                                            ("Class_Names", "Red", "Green", "Blue")}
            for f in table.get("features", [])}
    classes.write_text(json.dumps({"year": year, "source": SOURCE, "classes": rows}, indent=1),
                       encoding="utf-8")
    log.info("crop map %s: %s x %s px at %.0f m", year, round(width), round(height), cell_m)
    return path


def class_table(path: Path) -> dict[int, dict]:
    """Class names and USDA's colours for a fetched map."""
    year = Path(path).name.split("_")[1]
    table = json.loads((Path(path).parent / f"cdl_{year}_classes.json").read_text(encoding="utf-8"))
    return {int(k): v for k, v in table["classes"].items()}


# --------------------------------------------------------------------------- #
# Fields
# --------------------------------------------------------------------------- #


def nearest_town(lon: float, lat: float) -> tuple[str, float]:
    """The nearest Valley town and its distance in km."""
    best = min(RGV_TOWNS.items(), key=lambda kv: (kv[1][0] - lon) ** 2 * math.cos(
        math.radians(lat)) ** 2 + (kv[1][1] - lat) ** 2)
    dx = (best[1][0] - lon) * 111.32 * math.cos(math.radians(lat))
    dy = (best[1][1] - lat) * 110.57
    return best[0], math.hypot(dx, dy)


def trace(path: Path, crop: str, *, min_acres: float = 15.0, max_acres: float = 250.0,
          inset_m: float = 45.0, min_purity: float = 0.9,
          neck_m: float = NECK_M) -> list[Candidate]:
    """Every field of ``crop`` in the map, shrunk inside its edges, best first."""
    if crop not in CROP_CODES:
        raise CropMapError(f"no crop map classes for {crop!r}; choose from "
                           f"{', '.join(CROP_CODES)}")
    with rasterio.open(path) as dataset:
        classes, transform, crs = dataset.read(1), dataset.transform, dataset.crs
    mask = np.isin(classes, CROP_CODES[crop])
    cell = abs(transform.a)
    to_lonlat = Transformer.from_crs(crs, 4326, always_xy=True).transform
    found: list[Candidate] = []
    for geojson, _ in shapes(mask.astype(np.uint8), mask=mask, transform=transform,
                             connectivity=4):
        block = shape(geojson)
        if block.area / SQ_M_PER_ACRE < min_acres:
            continue
        inner = block.buffer(-inset_m).buffer(-neck_m).buffer(neck_m)
        if inner.is_empty:
            continue
        part = max(getattr(inner, "geoms", [inner]), key=lambda g: g.area).simplify(cell / 2)
        acres = part.area / SQ_M_PER_ACRE
        if not min_acres <= acres <= max_acres:
            continue
        inside = ~geometry_mask([mapping(part)], classes.shape, transform, all_touched=False)
        if not inside.any():
            continue
        purity = float(mask[inside].mean())
        if purity < min_purity:
            continue
        lonlat = shapely_transform(to_lonlat, part)
        town, km = nearest_town(lonlat.centroid.x, lonlat.centroid.y)
        found.append(Candidate(crop, lonlat, acres, purity,
                               part.area / part.minimum_rotated_rectangle.area, town, km))
    return sorted(found, key=lambda c: c.score, reverse=True)


def history(candidate: Candidate, years: Sequence[int], folder: Path) -> dict[int, float]:
    """For each earlier year, the share of the outline under its commonest class.

    Read from a small crop map around the outline per year, cached like the big one.
    Idle land counts as one class; cells the map left empty do not count.
    """
    west, south, east, north = candidate.geometry.bounds
    margin = 0.002          # about 200 m, so the outline sits well inside
    shares: dict[int, float] = {}
    for year in years:
        path = fetch((west - margin, south - margin, east + margin, north + margin), year, folder)
        with rasterio.open(path) as dataset:
            classes, transform, crs = dataset.read(1), dataset.transform, dataset.crs
        to_map = Transformer.from_crs(4326, crs, always_xy=True).transform
        outline = shapely_transform(to_map, candidate.geometry)
        inside = ~geometry_mask([mapping(outline)], classes.shape, transform, all_touched=False)
        values = classes[inside & (classes > 0)]
        if values.size:
            values = np.where(np.isin(values, list(IDLE_CODES)), 0, values)
            shares[year] = float(np.unique(values, return_counts=True)[1].max() / values.size)
    return shares


def pick(candidates: Sequence[Candidate], n: int, *, apart_km: float = 3.0,
         accept: Callable[[Candidate], bool] | None = None) -> list[Candidate]:
    """The best ``n`` that ``accept`` passes, no two within ``apart_km`` of each other."""
    chosen: list[Candidate] = []
    for candidate in candidates:
        c = candidate.geometry.centroid
        apart = all(math.hypot((c.x - o.geometry.centroid.x) * 111.32 * math.cos(math.radians(c.y)),
                               (c.y - o.geometry.centroid.y) * 110.57) >= apart_km for o in chosen)
        if apart and (accept is None or accept(candidate)):
            chosen.append(candidate)
        if len(chosen) == n:
            break
    return chosen


def features(chosen: Sequence[Candidate], *, year: int, prefix: str,
             history_years: Sequence[int] = ()) -> dict:
    """A fields.geojson the rest of the pipeline reads, one feature per field."""
    counts: dict[str, int] = {}
    items = []
    for c in chosen:
        counts[c.crop] = counts.get(c.crop, 0) + 1
        label = CROP_LABELS[c.crop]
        checked = (f", and one crop across it in each of {min(history_years)}-"
                   f"{max(history_years)}" if history_years and c.crop not in PERENNIAL else "")
        items.append({
            "type": "Feature",
            "properties": {
                "id": f"{prefix}-{c.crop}-{counts[c.crop]}",
                "name": f"{label.capitalize()} near {c.town}",
                "crop": label,
                "source": f"{SOURCE} {year}: {c.purity:.0%} of the outline mapped as "
                          f"{label}{checked}; traced inside the field's edge, not a survey",
            },
            "geometry": mapping(c.geometry),
        })
    return {"type": "FeatureCollection", "features": items}
