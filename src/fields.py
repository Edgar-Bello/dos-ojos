"""Field polygon input: parsing, validation, UTM projection and area reporting."""

from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from pyproj import Transformer
from shapely.geometry import MultiPolygon, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform
from shapely.validation import explain_validity, make_valid

from .config import SQ_M_PER_ACRE

log = logging.getLogger(__name__)

_POLYGONAL: frozenset[str] = frozenset({"Polygon", "MultiPolygon"})


class FieldValidationError(ValueError):
    """Raised when a fields file is malformed or holds an unusable geometry."""


@dataclass(frozen=True)
class Field:
    """One validated field polygon in WGS84, with its UTM zone and area."""

    field_id: str
    name: str
    crop: str
    geometry: BaseGeometry
    utm_epsg: int
    acres_computed: float
    acres_declared: float | None = None

    @property
    def geometry_wkt(self) -> str:
        """WGS84 geometry as WKT; this is what the cache stores."""
        return self.geometry.wkt

    @property
    def geom_hash(self) -> str:
        """Digest of the stored WKT, so a redrawn polygon invalidates its cache."""
        return hashlib.sha1(self.geometry_wkt.encode("utf-8")).hexdigest()

    @property
    def centroid_lonlat(self) -> tuple[float, float]:
        """Centroid as ``(lon, lat)`` in WGS84."""
        c = self.geometry.centroid
        return float(c.x), float(c.y)

    @property
    def acres_delta_pct(self) -> float | None:
        """Percent by which the computed area exceeds the declared area."""
        if not self.acres_declared:
            return None
        return 100.0 * (self.acres_computed - self.acres_declared) / self.acres_declared


# --------------------------------------------------------------------------- #
# Projection and area
# --------------------------------------------------------------------------- #


def utm_epsg_for(geom: BaseGeometry) -> int:
    """Return the EPSG code of the UTM zone containing the geometry's centroid.

    Rio Grande Valley fields land in zone 14N (EPSG:32614), which matches the
    ``proj:epsg`` the Sentinel-2 scenes there are delivered in.
    """
    centroid = geom.centroid
    zone = int(math.floor((centroid.x + 180.0) / 6.0)) + 1
    zone = min(max(zone, 1), 60)
    return (32600 if centroid.y >= 0 else 32700) + zone


def project_to_utm(geom: BaseGeometry, utm_epsg: int) -> BaseGeometry:
    """Reproject a WGS84 geometry into the given UTM CRS (metre units)."""
    transformer = Transformer.from_crs("EPSG:4326", f"EPSG:{utm_epsg}", always_xy=True)
    return shapely_transform(transformer.transform, geom)


def compute_area_m2(geom: BaseGeometry, utm_epsg: int) -> float:
    """Return the polygon area in square metres, measured in UTM."""
    return float(project_to_utm(geom, utm_epsg).area)


def compute_acres(geom: BaseGeometry, utm_epsg: int) -> float:
    """Return the polygon area in acres, measured in UTM."""
    return compute_area_m2(geom, utm_epsg) / SQ_M_PER_ACRE


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def _require_text(props: dict[str, Any], key: str, where: str) -> str:
    """Pull a required non-empty scalar property and return it as text."""
    value = props.get(key)
    if value is None:
        raise FieldValidationError(f"{where}: missing required property {key!r}")
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise FieldValidationError(
            f"{where}: property {key!r} must be text, got {type(value).__name__}"
        )
    text = str(value).strip()
    if not text:
        raise FieldValidationError(f"{where}: property {key!r} is empty")
    return text


def _optional_acres(props: dict[str, Any], where: str) -> float | None:
    """Pull the optional ``acres`` property, rejecting non-positive values."""
    value = props.get("acres")
    if value is None or value == "":
        return None
    try:
        acres = float(value)
    except (TypeError, ValueError) as exc:
        raise FieldValidationError(f"{where}: 'acres' is not a number: {value!r}") from exc
    if not math.isfinite(acres) or acres <= 0:
        raise FieldValidationError(f"{where}: 'acres' must be positive, got {acres}")
    return acres


def _polygonal_parts(geom: BaseGeometry) -> BaseGeometry | None:
    """Reduce a geometry to its polygonal parts, or ``None`` if it has none.

    ``make_valid`` can hand back a GeometryCollection mixing polygons with the
    lines and points it shed while repairing, and only the polygons are area.
    """
    if geom.geom_type in _POLYGONAL:
        return geom if not geom.is_empty else None
    if geom.geom_type == "GeometryCollection":
        polys = [g for g in geom.geoms if g.geom_type == "Polygon" and not g.is_empty]
        if polys:
            return polys[0] if len(polys) == 1 else MultiPolygon(polys)
    return None


def _validate_geometry(geom_json: dict[str, Any] | None, where: str) -> BaseGeometry:
    """Build a valid, non-empty, polygonal WGS84 geometry or raise."""
    if not geom_json:
        raise FieldValidationError(f"{where}: feature has no geometry")

    geom_type = geom_json.get("type")
    if geom_type not in _POLYGONAL:
        raise FieldValidationError(
            f"{where}: geometry must be Polygon or MultiPolygon, got {geom_type!r}"
        )

    try:
        geom = shape(geom_json)
    except Exception as exc:  # shapely raises assorted types for malformed input
        raise FieldValidationError(f"{where}: unreadable geometry: {exc}") from exc

    if geom.is_empty:
        raise FieldValidationError(f"{where}: geometry is empty")

    if not geom.is_valid:
        reason = explain_validity(geom)
        repaired = _polygonal_parts(make_valid(geom))
        if repaired is None:
            raise FieldValidationError(f"{where}: geometry is invalid and unrepairable ({reason})")
        log.warning("%s: repaired invalid geometry (%s)", where, reason)
        geom = repaired

    _check_wgs84_bounds(geom, where)
    return geom


def _check_wgs84_bounds(geom: BaseGeometry, where: str) -> None:
    """Reject coordinates outside WGS84 range, which usually means projected input."""
    min_x, min_y, max_x, max_y = geom.bounds
    if not (-180.0 <= min_x and max_x <= 180.0 and -90.0 <= min_y and max_y <= 90.0):
        raise FieldValidationError(
            f"{where}: coordinates {geom.bounds} fall outside WGS84 bounds. "
            "The file looks projected; reproject it to EPSG:4326 first."
        )


def _build_field(feature: dict[str, Any], where: str) -> Field:
    """Validate one GeoJSON feature and turn it into a :class:`Field`."""
    props = feature.get("properties") or {}
    if not isinstance(props, dict):
        raise FieldValidationError(f"{where}: 'properties' must be an object")

    geometry = _validate_geometry(feature.get("geometry"), where)
    utm_epsg = utm_epsg_for(geometry)
    return Field(
        field_id=_require_text(props, "id", where),
        name=_require_text(props, "name", where),
        crop=_require_text(props, "crop", where),
        geometry=geometry,
        utm_epsg=utm_epsg,
        acres_computed=compute_acres(geometry, utm_epsg),
        acres_declared=_optional_acres(props, where),
    )


def load_fields(path: Path) -> list[Field]:
    """Read, validate and return every field in a WGS84 GeoJSON FeatureCollection.

    Raises:
        FieldValidationError: if the file is malformed, holds no features, has a
            duplicate ``id``, or contains a geometry that cannot be repaired.
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FieldValidationError(f"{path}: not valid JSON ({exc})") from exc

    if not isinstance(raw, dict) or raw.get("type") != "FeatureCollection":
        raise FieldValidationError(f"{path}: expected a GeoJSON FeatureCollection")

    features = raw.get("features")
    if not isinstance(features, list) or not features:
        raise FieldValidationError(f"{path}: FeatureCollection has no features")

    fields: list[Field] = []
    seen: dict[str, int] = {}
    for index, feature in enumerate(features):
        where = f"{Path(path).name} feature[{index}]"
        if not isinstance(feature, dict):
            raise FieldValidationError(f"{where}: feature must be an object")
        field = _build_field(feature, where)
        if field.field_id in seen:
            raise FieldValidationError(
                f"{where}: duplicate field id {field.field_id!r} "
                f"(already used by feature[{seen[field.field_id]}])"
            )
        seen[field.field_id] = index
        fields.append(field)

    log.info("Loaded %d field(s) from %s", len(fields), path)
    return fields


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def format_field_table(fields: Sequence[Field]) -> str:
    """Render a fixed-width table of field areas for eyeballing against reality.

    The DIFF column compares computed area against the grower's declared acreage;
    anything past a few percent usually means the polygon is drawn wrong.
    """
    if not fields:
        return "(no fields)"

    headers = ("FIELD ID", "NAME", "CROP", "UTM", "ACRES", "DECLARED", "DIFF")
    rows: list[tuple[str, ...]] = []
    for field in fields:
        delta = field.acres_delta_pct
        rows.append(
            (
                field.field_id,
                field.name,
                field.crop,
                str(field.utm_epsg),
                f"{field.acres_computed:.2f}",
                f"{field.acres_declared:.1f}" if field.acres_declared else "-",
                f"{delta:+.1f}%" if delta is not None else "-",
            )
        )

    total_computed = sum(f.acres_computed for f in fields)
    declared = [f.acres_declared for f in fields if f.acres_declared]
    total_declared = sum(declared) if declared else None
    total_delta = (
        100.0 * (total_computed - total_declared) / total_declared if total_declared else None
    )
    footer = (
        f"{len(fields)} field(s)",
        "",
        "",
        "",
        f"{total_computed:.2f}",
        f"{total_declared:.1f}" if total_declared else "-",
        f"{total_delta:+.1f}%" if total_delta is not None else "-",
    )

    widths = [
        max(len(headers[i]), len(footer[i]), *(len(r[i]) for r in rows))
        for i in range(len(headers))
    ]
    # Numeric columns read better right-aligned.
    aligns = ["<", "<", "<", ">", ">", ">", ">"]

    def render(cells: Sequence[str]) -> str:
        return "  ".join(f"{c:{a}{w}}" for c, a, w in zip(cells, aligns, widths)).rstrip()

    rule = "  ".join("-" * w for w in widths)
    lines = [render(headers), rule]
    lines += [render(r) for r in rows]
    lines += [rule, render(footer)]
    return "\n".join(lines)
