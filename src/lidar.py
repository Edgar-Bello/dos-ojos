"""A field's ground and surface from USGS 3DEP airborne LiDAR: no drone needed.

The US Geological Survey's 3D Elevation Program flies laser scanners over most
of the country and puts the results in the public domain. Microsoft's Planetary
Computer serves them as cloud-optimised GeoTIFFs at 2 m: a ground model (DTM,
the bare earth under the crop) and a surface model (DSM, the first thing the
laser hit). Only the field's window is read, a few hundred kilobytes, and the
terrain step then judges that ground exactly as it judges a drone's.

It is older and coarser than a drone. The Valley was flown in 2018-19, so a
field leveled since will not show it, and a 2 m cell sees broad high and low
spots but not a single furrow. Good for a first look at whether the water can
cross a field evenly; a bare-soil drone flight is still the sharper check.
"""

from __future__ import annotations

import json
import logging
import math
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import rasterio
from rasterio.merge import merge
from rasterio.warp import transform_bounds
from shapely.geometry.base import BaseGeometry

log = logging.getLogger(__name__)

STAC_SEARCH = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
TOKEN = "https://planetarycomputer.microsoft.com/api/sas/v1/token/{collection}"
COLLECTIONS = {"dtm": "3dep-lidar-dtm", "dsm": "3dep-lidar-dsm"}
#: Ground kept around the field, so its edge cells have neighbours.
MARGIN_M = 20.0
SOURCE = "USGS 3DEP lidar (public domain), via Microsoft Planetary Computer"

_GDAL = {"GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR", "GDAL_HTTP_MAX_RETRY": "4",
         "GDAL_HTTP_RETRY_DELAY": "2", "GDAL_HTTP_TIMEOUT": "60"}

Fetcher = Callable[[str, bytes | None], dict]


class LidarError(RuntimeError):
    """Raised when no LiDAR covers the field or it cannot be read."""


@dataclass(frozen=True)
class LidarGround:
    """The two surfaces written for a field, and where they came from."""

    dtm: Path
    dsm: Path
    project: str          #: the USGS survey, e.g. TX_South_B8_2018
    year: int
    resolution_m: float
    tiles: int


def _http_json(url: str, body: bytes | None = None) -> dict:
    request = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json", "User-Agent": "dosojos-drone (lidar ground)"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def search(bbox: tuple[float, float, float, float], collection: str, *,
           fetch: Fetcher | None = None) -> list[dict]:
    """The STAC items of ``collection`` touching a lon/lat box."""
    fetch = fetch or _http_json
    body = json.dumps({"collections": [collection], "bbox": list(bbox), "limit": 100}).encode()
    return fetch(STAC_SEARCH, body).get("features", [])


def _signed(href: str, token: str) -> str:
    return f"{href}{'&' if '?' in href else '?'}{token}"


def _project(item: dict) -> str:
    """'USGS_LPC_TX_South_B8_2018_LAS_2019-dtm-2m-5-7' -> 'TX_South_B8_2018'."""
    name = item["id"].split("-")[0]
    return name.removeprefix("USGS_LPC_").rsplit("_LAS_", 1)[0] or name


def fetch_ground(outline: BaseGeometry, folder: Path, *, fetch: Fetcher | None = None,
                 opener: Callable[[str], rasterio.DatasetReader] | None = None) -> LidarGround:
    """Write ``dtm.tif`` and ``dsm.tif`` for a lon/lat outline into ``folder``.

    Raises:
        LidarError: if no survey covers the field, or the tiles cannot be read.
    """
    fetch = fetch or _http_json
    opener = opener or rasterio.open
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    # Degrees of margin, generous; the exact window is cut in metres below.
    pad = MARGIN_M / 111_000 * 2
    west, south, east, north = outline.bounds
    bbox = (west - pad, south - pad, east + pad, north + pad)
    written: dict[str, Path] = {}
    project, year, resolution, tiles = "", 0, math.nan, 0
    for kind, collection in COLLECTIONS.items():
        try:
            items = search(bbox, collection, fetch=fetch)
            token = fetch(TOKEN.format(collection=collection), None)["token"]
        except (OSError, KeyError, ValueError) as exc:
            raise LidarError(f"could not reach the Planetary Computer for {kind}: {exc}") from exc
        if not items:
            raise LidarError(
                "no USGS 3DEP lidar covers this field (most of the lower 48 is flown; "
                "check the outline). A bare-soil drone flight is the other way to map the ground."
            )
        # The newest survey wins where two overlap: the ground it saw is the latest.
        newest = max(str(i["properties"].get("start_datetime") or i["properties"].get("datetime")
                         or "")[:4] for i in items)
        items = [i for i in items if str(i["properties"].get("start_datetime")
                                         or i["properties"].get("datetime") or "")[:4] == newest]
        project, year, tiles = _project(items[0]), int(newest or 0), len(items)
        with rasterio.Env(**_GDAL):
            try:
                sources = [opener(_signed(i["assets"]["data"]["href"], token)) for i in items]
            except rasterio.RasterioIOError as exc:
                raise LidarError(f"could not open the lidar tiles: {exc}") from exc
            try:
                crs = sources[0].crs
                if any(s.crs != crs for s in sources):
                    raise LidarError("the lidar tiles for this field are in different "
                                     "coordinate systems")
                left, bottom, right, top = transform_bounds("EPSG:4326", crs, west, south, east,
                                                            north, densify_pts=21)
                resolution = abs(sources[0].res[0])
                mosaic, transform = merge(
                    sources, bounds=(left - MARGIN_M, bottom - MARGIN_M, right + MARGIN_M,
                                     top + MARGIN_M), nodata=np.nan, dtype="float32")
            except rasterio.RasterioIOError as exc:
                raise LidarError(f"could not read the lidar tiles: {exc}") from exc
            finally:
                for source in sources:
                    source.close()
        if not np.isfinite(mosaic).any():
            raise LidarError(f"the lidar {kind} holds no values over this field")
        path = folder / f"{kind}.tif"
        with rasterio.open(path, "w", driver="GTiff", height=mosaic.shape[1],
                           width=mosaic.shape[2], count=1, dtype="float32", crs=crs,
                           transform=transform, nodata=np.nan, compress="deflate") as out:
            out.write(mosaic[0], 1)
        written[kind] = path
        log.info("lidar %s: %s x %s cells at %.1f m from %d tile(s) of %s", kind,
                 mosaic.shape[2], mosaic.shape[1], resolution, len(items), project)
    (folder / "source.json").write_text(json.dumps({
        "source": SOURCE, "project": project, "year": year, "resolution_m": resolution,
        "tiles": tiles, "collections": list(COLLECTIONS.values()),
    }, indent=2), encoding="utf-8")
    return LidarGround(written["dtm"], written["dsm"], project, year, resolution, tiles)


#: Past this share of cells with something over 1 m above the ground, the ground
#: under it is mostly interpolated between the few returns that reached the soil.
COVERED_WARN = 0.15


def covered_share(ground: LidarGround, *, above_m: float = 1.0) -> float:
    """Share of the field where the laser's first return was over ``above_m`` up."""
    with rasterio.open(ground.dtm) as dtm, rasterio.open(ground.dsm) as dsm:
        height = dsm.read(1) - dtm.read(1)
    valid = np.isfinite(height)
    return float((height[valid] > above_m).mean()) if valid.any() else 0.0
