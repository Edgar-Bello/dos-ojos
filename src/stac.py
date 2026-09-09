"""STAC search and windowed Cloud-Optimized GeoTIFF reads.

Only the pixels inside a field polygon are ever requested: ``odc-stac`` turns the
field outline into a small geobox and GDAL fetches the matching byte ranges out
of each COG. Whole scenes are never downloaded.
"""

from __future__ import annotations

import logging
import os
import random
import socket
import time
from dataclasses import dataclass, field as dc_field
from datetime import date, datetime
from typing import Any, Callable, Literal, Mapping, Sequence, TypeVar

import numpy as np
import pystac
from affine import Affine
from odc.stac import load as odc_load, parse_items
from pystac_client import Client
from rasterio.features import geometry_mask
from shapely.geometry import mapping, shape

from . import indices
from .config import (
    BAND_ASSETS,
    DEFAULT_NODATA,
    DEFAULT_OFFSET,
    DEFAULT_SCALE,
    Settings,
)
from .fields import Field, project_to_utm

log = logging.getLogger(__name__)

T = TypeVar("T")

ClipStatus = Literal["kept", "dropped", "error"]

#: SCL holds class numbers, so interpolating it would invent classes that do not
#: exist. Everything else is continuous reflectance and resamples smoothly.
RESAMPLING: dict[str, str] = {"scl": "nearest", "*": "bilinear"}

#: GDAL settings that make range reads against public S3 COGs fast and resilient.
_GDAL_ENV: dict[str, str] = {
    "AWS_NO_SIGN_REQUEST": "YES",
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
    "GDAL_HTTP_MAX_RETRY": "5",
    "GDAL_HTTP_RETRY_DELAY": "1",
    "GDAL_HTTP_TIMEOUT": "60",
    "VSI_CACHE": "TRUE",
    "VSI_CACHE_SIZE": "67108864",
}


class StacUnavailableError(RuntimeError):
    """The STAC API or a COG could not be reached after retrying."""


class OfflineViolation(RuntimeError):
    """Something tried to use the network while ``--offline`` was in force."""


# --------------------------------------------------------------------------- #
# Data carriers
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SceneRef:
    """One STAC item, with the metadata we care about pulled to the surface."""

    scene_id: str
    solar_date: date
    datetime_utc: datetime
    platform: str | None
    mgrs_tile: str | None
    epsg: int | None
    cloud_cover: float | None
    processing_baseline: str | None
    #: True when the COG mirror already folded ESA's -0.1 BOA offset into the pixels.
    boa_offset_applied: bool = False
    item: pystac.Item = dc_field(repr=False, compare=False, default=None)


@dataclass(frozen=True)
class Clip:
    """Every band for one field on one solar day, at 10 m, on a common grid."""

    field_id: str
    solar_date: date
    scene_ids: tuple[str, ...]
    bands: dict[str, np.ndarray]   # 'B03'..'B11', float32 reflectance, NaN at nodata
    scl: np.ndarray               # uint8 scene classification
    poly_mask: np.ndarray         # bool, True for pixels inside the field
    transform: Affine
    epsg: int

    @property
    def n_total_px(self) -> int:
        """Pixel count inside the field outline, the denominator of valid_fraction."""
        return int(np.count_nonzero(self.poly_mask))


@dataclass(frozen=True)
class ClipOutcome:
    """The result of trying to build one observation, kept or not.

    Mirrors a row of the ``fetch_log`` table, so every attempt is explainable
    after the fact.
    """

    field_id: str
    solar_date: date
    scene_ids: tuple[str, ...]
    status: ClipStatus
    reason: str
    valid_fraction: float
    n_valid_px: int
    n_total_px: int
    duration_ms: int
    nodata_fraction: float = 0.0
    clip: Clip | None = None
    mask: np.ndarray | None = None

    @property
    def scene_id_key(self) -> str:
        """Scene ids joined for storage, since a solar day can merge several tiles."""
        return "+".join(self.scene_ids)


# --------------------------------------------------------------------------- #
# Network policy
# --------------------------------------------------------------------------- #


def configure_gdal_env() -> None:
    """Tune GDAL for anonymous range reads, without clobbering the user's settings."""
    for key, value in _GDAL_ENV.items():
        os.environ.setdefault(key, value)


def assert_online(settings: Settings, what: str) -> None:
    """Raise :class:`OfflineViolation` if ``what`` needs a network we are not allowed."""
    if settings.offline:
        raise OfflineViolation(
            f"--offline is set, so it is not possible to {what}. "
            "Run a fetch while connected to populate the cache first."
        )


_guard_installed = False


def install_offline_guard() -> None:
    """Make every non-loopback TCP connection raise, as a backstop for ``--offline``.

    This covers the STAC search path, which reaches the network through Python's
    socket module. COG reads go out through GDAL's own C-level HTTP client and
    never pass through here, so those are gated by :func:`assert_online` instead.
    """
    global _guard_installed
    if _guard_installed:
        return

    real_connect = socket.socket.connect
    real_create_connection = socket.create_connection

    def guarded_connect(self: socket.socket, address: Any, *args: Any, **kwargs: Any) -> Any:
        _refuse(address)
        return real_connect(self, address, *args, **kwargs)

    def guarded_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
        _refuse(address)
        return real_create_connection(address, *args, **kwargs)

    socket.socket.connect = guarded_connect  # type: ignore[method-assign]
    socket.create_connection = guarded_create_connection  # type: ignore[assignment]
    _guard_installed = True
    log.debug("Offline guard installed: non-loopback connections will raise")


def _refuse(address: Any) -> None:
    """Raise unless the address is loopback."""
    host = address[0] if isinstance(address, (tuple, list)) else address
    text = str(host)
    if text in {"localhost", "::1"} or text.startswith("127."):
        return
    raise OfflineViolation(
        f"--offline is set but something attempted a network connection to {text!r}"
    )


def with_retry(
    fn: Callable[[], T],
    *,
    what: str,
    attempts: int = 4,
    base_delay: float = 1.0,
) -> T:
    """Call ``fn``, retrying transient failures with exponential backoff and jitter.

    Raises:
        StacUnavailableError: once every attempt has failed, naming the last error.
        OfflineViolation: immediately, since retrying an offline breach is pointless.
    """
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except OfflineViolation:
            raise
        except Exception as exc:  # noqa: BLE001 - retry policy is deliberately broad
            last_error = exc
            if attempt == attempts:
                break
            if _is_permanent(exc):
                log.debug("%s hit a permanent failure, not retrying: %s", what, exc)
                break
            delay = base_delay * (2 ** (attempt - 1)) * (1.0 + random.random() * 0.25)
            log.warning(
                "%s failed (attempt %d/%d): %s - retrying in %.1fs",
                what, attempt, attempts, exc, delay,
            )
            time.sleep(delay)
    raise StacUnavailableError(f"{what} failed after {attempts} attempt(s): {last_error}")


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #


#: Failures that will never succeed on a retry, so backing off just wastes time.
_PERMANENT_ERROR_MARKERS: tuple[str, ...] = (
    "does not exist in the file system",
    "not recognized as a supported dataset",
    "access denied",
    "no such file",
)


def _is_permanent(exc: Exception) -> bool:
    """True for failures a retry cannot fix, such as a missing or private object."""
    text = str(exc).lower()
    return any(marker in text for marker in _PERMANENT_ERROR_MARKERS)


def open_client(settings: Settings) -> Client:
    """Open a STAC API client, retrying if the endpoint is briefly unreachable."""
    assert_online(settings, "open the STAC API")
    configure_gdal_env()
    return with_retry(
        lambda: Client.open(settings.stac_url),
        what=f"opening STAC API at {settings.stac_url}",
    )


def search_scenes(
    field: Field,
    start: date,
    end: date,
    settings: Settings,
    *,
    client: Client | None = None,
) -> list[SceneRef]:
    """Return every scene intersecting ``field`` between ``start`` and ``end``.

    Scenes above ``settings.max_scene_cloud`` are filtered out server-side to cut
    down on reads. That is only a coarse prefilter; whether a *field* is usable is
    decided later by its own SCL mask.
    """
    assert_online(settings, f"search scenes for field {field.field_id}")
    client = client or open_client(settings)

    def run() -> list[pystac.Item]:
        search = client.search(
            collections=[settings.collection],
            intersects=field.geometry,
            datetime=f"{start.isoformat()}/{end.isoformat()}",
            query={"eo:cloud_cover": {"lt": settings.max_scene_cloud}},
        )
        return list(search.items())

    items = with_retry(run, what=f"STAC search for {field.field_id}")
    scenes = _to_scene_refs(items)
    log.info(
        "%s: found %d item(s) across %d solar day(s) in %s..%s",
        field.field_id, len(scenes), len(group_by_solar_day(scenes)), start, end,
    )
    return scenes


def _to_scene_refs(items: Sequence[pystac.Item]) -> list[SceneRef]:
    """Wrap items as :class:`SceneRef`, using odc's own solar-date calculation.

    Reusing ``parse_items`` guarantees our grouping matches the ``groupby``
    ``odc-stac`` applies internally, so scene ids never drift from pixels.
    """
    if not items:
        return []
    parsed = list(parse_items(items))
    scenes = [
        SceneRef(
            scene_id=item.id,
            solar_date=meta.solar_date.date(),
            datetime_utc=item.datetime,
            platform=item.properties.get("platform"),
            mgrs_tile=item.properties.get("grid:code"),
            epsg=_epsg_of(item.properties),
            cloud_cover=item.properties.get("eo:cloud_cover"),
            processing_baseline=item.properties.get("s2:processing_baseline"),
            boa_offset_applied=bool(item.properties.get("earthsearch:boa_offset_applied")),
            item=item,
        )
        for item, meta in zip(items, parsed)
    ]
    return sorted(scenes, key=lambda s: (s.solar_date, s.scene_id))


def _epsg_of(props: Mapping[str, Any]) -> int | None:
    """Read the scene EPSG from either the old or the new projection extension key."""
    value = props.get("proj:epsg")
    if value is not None:
        return int(value)
    code = props.get("proj:code")
    if isinstance(code, str) and code.upper().startswith("EPSG:"):
        return int(code.split(":", 1)[1])
    return None


def select_covering_items(
    field: Field, day_scenes: Sequence[SceneRef]
) -> list[SceneRef]:
    """Reduce one solar day's scenes to the fewest that actually cover the field.

    Sentinel-2 MGRS tiles overlap by roughly 10 km, so a field can sit wholly
    inside four tiles of the same acquisition. Those tiles show identical ground,
    and reading all of them costs four times the requests for no extra pixels.
    Prefer a single tile containing the whole field, taking the one with least
    nodata; fall back to a greedy union only when the field really straddles a seam.
    """
    usable = [scene for scene in day_scenes if is_readable(scene)] or list(day_scenes)
    if len(usable) <= 1:
        return usable

    covering = [s for s in usable if shape(s.item.geometry).contains(field.geometry)]
    if covering:
        return [min(covering, key=_tile_preference)]

    ranked = sorted(usable, key=lambda s: _overlap_area(field, s), reverse=True)
    chosen: list[SceneRef] = []
    covered = None
    for scene in ranked:
        if covered is not None and covered.contains(field.geometry):
            break
        contribution = shape(scene.item.geometry).intersection(field.geometry)
        if contribution.is_empty:
            continue
        if covered is not None and contribution.difference(covered).is_empty:
            continue
        chosen.append(scene)
        covered = contribution if covered is None else covered.union(contribution)
    return chosen or list(day_scenes)


def is_readable(scene: SceneRef) -> bool:
    """True when every band of a scene is a public Cloud-Optimized GeoTIFF.

    A minority of items in the collection point at the requester-pays JPEG 2000
    originals rather than the free COG mirror. Those cannot be read anonymously,
    and a JP2 is not range-readable in any case, so another tile from the same
    pass is a better choice when one exists.
    """
    for asset_key in BAND_ASSETS.values():
        asset = scene.item.assets.get(asset_key)
        if asset is None:
            return False
        href = str(asset.href).lower()
        if href.startswith("s3://") or href.endswith(".jp2"):
            return False
    return True


def _tile_preference(scene: SceneRef) -> tuple[float, float]:
    """Rank covering tiles: least nodata first, then least cloud."""
    nodata = scene.item.properties.get("s2:nodata_pixel_percentage")
    return (
        float(nodata) if nodata is not None else 100.0,
        float(scene.cloud_cover) if scene.cloud_cover is not None else 100.0,
    )


def _overlap_area(field: Field, scene: SceneRef) -> float:
    """Area of the field covered by a scene's footprint, in degrees squared."""
    return float(shape(scene.item.geometry).intersection(field.geometry).area)


def group_by_solar_day(scenes: Sequence[SceneRef]) -> dict[date, list[SceneRef]]:
    """Group scenes by solar day, oldest first.

    A field sitting on an MGRS tile seam is covered by several items on the same
    pass. Grouping them yields one complete observation instead of two partial ones.
    """
    groups: dict[date, list[SceneRef]] = {}
    for scene in scenes:
        groups.setdefault(scene.solar_date, []).append(scene)
    return dict(sorted(groups.items()))


# --------------------------------------------------------------------------- #
# Windowed reads
# --------------------------------------------------------------------------- #


def load_clip(field: Field, scenes: Sequence[SceneRef], settings: Settings) -> Clip:
    """Read all bands for one field on one solar day, clipped to the polygon.

    Bands come back as 10 m float32 surface reflectance with NaN at nodata; B11
    and SCL are upsampled from 20 m so every index aligns pixel for pixel.
    """
    assert_online(settings, f"read imagery for field {field.field_id}")
    if not scenes:
        raise ValueError("load_clip needs at least one scene")

    solar_date = scenes[0].solar_date
    items = [scene.item for scene in scenes]

    dataset = with_retry(
        lambda: odc_load(
            items,
            bands=tuple(BAND_ASSETS.values()),
            geopolygon=field.geometry,
            crs=f"epsg:{field.utm_epsg}",
            resolution=settings.target_res_m,
            groupby="solar_day",
            resampling=RESAMPLING,
            chunks=None,
            fail_on_error=True,
        ),
        what=f"COG read for {field.field_id} on {solar_date}",
    )

    n_times = int(dataset.sizes.get("time", 0))
    if n_times != 1:
        raise StacUnavailableError(
            f"{field.field_id} {solar_date}: expected one solar day, got {n_times}"
        )
    day = dataset.isel(time=0)

    scl = np.asarray(day[BAND_ASSETS["SCL"]].values).astype(np.uint8)
    bands = {
        band: _to_reflectance(np.asarray(day[asset].values), *_reflectance_transform(scenes, asset))
        for band, asset in BAND_ASSETS.items()
        if band != "SCL"
    }

    geobox = day.odc.geobox
    poly_mask = polygon_mask(field, geobox.transform, scl.shape)
    warn_if_implausible(bands, poly_mask, field.field_id, solar_date)
    return Clip(
        field_id=field.field_id,
        solar_date=solar_date,
        scene_ids=tuple(scene.scene_id for scene in scenes),
        bands=bands,
        scl=scl,
        poly_mask=poly_mask,
        transform=geobox.transform,
        epsg=field.utm_epsg,
    )


#: Above this share of negative pixels, the scaling is wrong rather than the sky.
MAX_NEGATIVE_FRACTION = 0.25


def warn_if_implausible(
    bands: dict[str, np.ndarray], poly_mask: np.ndarray, field_id: str, solar_date: date
) -> None:
    """Log loudly when a band comes back mostly negative.

    Surface reflectance cannot be negative. Atmospheric correction leaves a few
    percent of dark-canopy pixels slightly below zero, but a quarter of a field
    going negative means the scale or offset has been misapplied, which otherwise
    corrupts every index quietly and plausibly.
    """
    for name, values in bands.items():
        inside = values[poly_mask]
        finite = inside[np.isfinite(inside)]
        if finite.size == 0:
            continue
        negative = float(np.count_nonzero(finite < 0.0)) / finite.size
        if negative > MAX_NEGATIVE_FRACTION:
            log.warning(
                "%s %s: %.0f%% of %s is negative reflectance - the scale/offset "
                "handling for this item looks wrong",
                field_id, solar_date, 100 * negative, name,
            )


def polygon_mask(
    field: Field, transform: Affine, shape: tuple[int, ...]
) -> np.ndarray:
    """Rasterise the field outline onto the clip grid; True means inside the field.

    Pixel centres decide membership. If the field is so small that no centre falls
    inside, every touched pixel counts instead, so a tiny plot still yields data.
    """
    geom = mapping(project_to_utm(field.geometry, field.utm_epsg))
    out_shape = (int(shape[0]), int(shape[1]))
    mask = geometry_mask([geom], out_shape=out_shape, transform=transform, invert=True)
    if not mask.any():
        log.warning(
            "%s: no pixel centre falls inside the field; falling back to all_touched",
            field.field_id,
        )
        mask = geometry_mask(
            [geom], out_shape=out_shape, transform=transform, invert=True, all_touched=True
        )
    return mask


def _reflectance_transform(
    scenes: Sequence[SceneRef], asset_key: str
) -> tuple[float, float, float]:
    """Return ``(scale, offset, nodata)`` for an asset.

    From processing baseline 04.00, ESA's L2A products carry a -0.1 reflectance
    offset, and ``raster:bands`` on these items always declares it. But the Earth
    Search COG mirror harmonises the pixel values when it converts them, which it
    reports as ``earthsearch:boa_offset_applied``. When that flag is set the
    declared offset describes ESA's original convention rather than the bytes in
    the file, and re-applying it would drive most of a field's green and red
    reflectance negative. The flag is therefore the authority, not the offset.
    """
    for scene in scenes:
        asset = scene.item.assets.get(asset_key)
        if asset is None:
            continue
        raster = asset.extra_fields.get("raster:bands") or asset.extra_fields.get("bands")
        if isinstance(raster, list) and raster and isinstance(raster[0], dict):
            entry = raster[0]
            offset = float(entry.get("offset", DEFAULT_OFFSET))
            if scene.boa_offset_applied:
                offset = 0.0
            return (
                float(entry.get("scale", DEFAULT_SCALE)),
                offset,
                float(entry.get("nodata", DEFAULT_NODATA)),
            )
    log.debug("no raster:bands on asset %r; using L2A defaults", asset_key)
    default_offset = 0.0 if all(s.boa_offset_applied for s in scenes) else DEFAULT_OFFSET
    return DEFAULT_SCALE, default_offset, DEFAULT_NODATA


def _to_reflectance(
    raw: np.ndarray, scale: float, offset: float, nodata: float
) -> np.ndarray:
    """Convert raw DN to surface reflectance, with NaN where the sensor saw nothing.

    Nodata is knocked out *before* scaling, otherwise a nodata 0 would silently
    become a plausible-looking -0.1 reflectance.
    """
    values = raw.astype(np.float32)
    values[raw == nodata] = np.nan
    return values * scale + offset


# --------------------------------------------------------------------------- #
# One observation, end to end
# --------------------------------------------------------------------------- #


def load_and_mask(
    field: Field, day_scenes: Sequence[SceneRef], settings: Settings
) -> ClipOutcome:
    """Read one solar day for one field and decide whether it is usable.

    Reads only the tiles needed to cover the field. If the chosen tile fails to
    read or turns out to be mostly nodata here, the next covering tile is tried
    before the day is written off: overlapping tiles of one pass differ in where
    their data ends, but not in their weather. A genuinely cloudy day therefore
    stops immediately, since every tile of that pass saw the same sky.

    Every attempt logs one line naming the field, date, scenes, valid fraction and
    the keep-or-drop decision with its reason.
    """
    if not any(is_readable(scene) for scene in day_scenes):
        return _unreadable_day(field, day_scenes)

    outcome: ClipOutcome | None = None
    attempts = _read_order(field, day_scenes)
    for position, scenes in enumerate(attempts, start=1):
        outcome = _attempt_day(field, scenes, settings)
        if outcome.status == "kept":
            break
        if not (outcome.status == "error" or _nodata_dominated(outcome)):
            break  # genuinely cloudy: another tile of the same pass sees the same sky
        if position < len(attempts):
            trouble = "failed to read" if outcome.status == "error" else "is mostly nodata"
            log.info(
                "%s %s: tile %s, trying the next tile",
                field.field_id, outcome.solar_date, trouble,
            )
    assert outcome is not None  # _read_order never returns an empty list
    return outcome


def _unreadable_day(field: Field, day_scenes: Sequence[SceneRef]) -> ClipOutcome:
    """Record a day whose every tile is requester-pays JP2, without attempting it.

    Dropped rather than errored on purpose: the day will never become readable, so
    marking it resolved stops every later run from retrying a guaranteed failure.
    """
    outcome = ClipOutcome(
        field_id=field.field_id,
        solar_date=day_scenes[0].solar_date,
        scene_ids=tuple(scene.scene_id for scene in day_scenes),
        status="dropped",
        reason="no public COG for this day; only requester-pays JP2 assets",
        valid_fraction=0.0,
        n_valid_px=0,
        n_total_px=0,
        duration_ms=0,
    )
    _log_outcome(outcome)
    return outcome


def _read_order(
    field: Field, day_scenes: Sequence[SceneRef]
) -> list[list[SceneRef]]:
    """Scene sets to try for one day: the minimal cover, then nodata fallbacks."""
    primary = select_covering_items(field, day_scenes)
    order = [primary]
    if len(primary) == 1:
        already = {scene.scene_id for scene in primary}
        alternatives = [
            scene
            for scene in day_scenes
            if scene.scene_id not in already
            and is_readable(scene)
            and shape(scene.item.geometry).contains(field.geometry)
        ]
        order.extend([scene] for scene in sorted(alternatives, key=_tile_preference))
    return order


def _nodata_dominated(outcome: ClipOutcome) -> bool:
    """True when missing data, rather than cloud, is why an observation failed."""
    return outcome.status == "dropped" and outcome.nodata_fraction > 0.2


def _attempt_day(
    field: Field, day_scenes: Sequence[SceneRef], settings: Settings
) -> ClipOutcome:
    """Read one specific set of scenes for a day and score the result."""
    started = time.monotonic()
    solar_date = day_scenes[0].solar_date
    scene_ids = tuple(scene.scene_id for scene in day_scenes)

    try:
        clip = load_clip(field, day_scenes, settings)
    except OfflineViolation:
        raise
    except Exception as exc:  # noqa: BLE001 - any read failure is a logged outcome
        outcome = ClipOutcome(
            field_id=field.field_id,
            solar_date=solar_date,
            scene_ids=scene_ids,
            status="error",
            reason=f"read failed: {exc}",
            valid_fraction=0.0,
            n_valid_px=0,
            n_total_px=0,
            duration_ms=_elapsed_ms(started),
        )
        _log_outcome(outcome)
        return outcome

    mask = indices.observation_mask(clip.scl, clip.bands.values(), clip.poly_mask)
    n_total = clip.n_total_px
    n_valid = int(np.count_nonzero(mask))
    fraction = indices.valid_fraction(mask, clip.poly_mask)
    nodata_fraction = indices.class_fraction(clip.scl, clip.poly_mask, 0)

    keep = fraction >= settings.min_valid_fraction
    reason = "" if keep else _drop_reason(fraction, nodata_fraction, settings)
    outcome = ClipOutcome(
        field_id=field.field_id,
        solar_date=solar_date,
        scene_ids=scene_ids,
        status="kept" if keep else "dropped",
        reason=reason,
        valid_fraction=fraction,
        n_valid_px=n_valid,
        n_total_px=n_total,
        duration_ms=_elapsed_ms(started),
        nodata_fraction=nodata_fraction,
        clip=clip if keep else None,
        mask=mask if keep else None,
    )
    _log_outcome(outcome)
    return outcome


def _drop_reason(fraction: float, nodata_fraction: float, settings: Settings) -> str:
    """Explain a rejection, naming cloud or missing data as the culprit."""
    cause = "mostly nodata" if nodata_fraction > 0.2 else "cloud or shadow"
    return (
        f"valid_fraction {fraction:.2f} below threshold "
        f"{settings.min_valid_fraction:.2f} ({cause})"
    )


def _elapsed_ms(started: float) -> int:
    """Milliseconds since a ``time.monotonic()`` mark."""
    return int((time.monotonic() - started) * 1000)


def _log_outcome(outcome: ClipOutcome) -> None:
    """Emit the one-line audit record for a fetch attempt."""
    log.info(
        "fetch %s %s scenes=%s valid=%.3f (%d/%d px) %s%s [%dms]",
        outcome.field_id,
        outcome.solar_date,
        outcome.scene_id_key,
        outcome.valid_fraction,
        outcome.n_valid_px,
        outcome.n_total_px,
        outcome.status.upper(),
        f" - {outcome.reason}" if outcome.reason else "",
        outcome.duration_ms,
    )
