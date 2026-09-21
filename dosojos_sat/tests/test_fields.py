"""Tests for field parsing, validation and UTM area math."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pyproj import Transformer
from shapely.geometry import Polygon, box, mapping

from dosojos_sat.fields import (
    FieldValidationError,
    compute_acres,
    load_fields,
    utm_epsg_for,
)

# A square of exactly 1_000_000 m^2 in UTM 14N, expressed in WGS84.
_UTM14_TO_WGS84 = Transformer.from_crs("EPSG:32614", "EPSG:4326", always_xy=True)
_KNOWN_SQUARE = Polygon(
    [_UTM14_TO_WGS84.transform(x, y) for x, y in box(600000, 2899000, 601000, 2900000).exterior.coords]
)
_KNOWN_ACRES = 1_000_000 / 4046.8564224  # 247.1054


def _collection(*features: dict[str, Any]) -> dict[str, Any]:
    """Wrap features in a FeatureCollection."""
    return {"type": "FeatureCollection", "features": list(features)}


def _feature(geometry: Any = _KNOWN_SQUARE, **props: Any) -> dict[str, Any]:
    """Build a valid feature, overriding properties as needed."""
    base = {"id": "f1", "name": "Test Field", "crop": "sorghum"}
    base.update(props)
    return {"type": "Feature", "properties": base, "geometry": mapping(geometry)}


def _write(tmp_path: Path, payload: dict[str, Any]) -> Path:
    """Serialise a GeoJSON payload to a temp file and return its path."""
    path = tmp_path / "fields.geojson"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Area and projection
# --------------------------------------------------------------------------- #


def test_utm_zone_for_rio_grande_valley() -> None:
    """RGV fields must resolve to zone 14N, matching the Sentinel-2 scene CRS."""
    assert utm_epsg_for(_KNOWN_SQUARE) == 32614


@pytest.mark.parametrize(
    ("lon", "lat", "expected"),
    [
        (-97.97, 26.20, 32614),   # Rio Grande Valley
        (-123.1, 49.28, 32610),   # Vancouver, zone 10N
        (151.2, -33.87, 32756),   # Sydney, zone 56S
        (-179.9, 0.5, 32601),     # first zone, just north of the equator
        (179.9, -0.5, 32760),     # last zone, just south
    ],
)
def test_utm_zone_boundaries(lon: float, lat: float, expected: int) -> None:
    """Zone selection is correct across hemispheres and at the antimeridian."""
    point_area = box(lon - 0.001, lat - 0.001, lon + 0.001, lat + 0.001)
    assert utm_epsg_for(point_area) == expected


def test_area_matches_known_square() -> None:
    """A 1000 m x 1000 m square must measure 247.1054 acres."""
    acres = compute_acres(_KNOWN_SQUARE, 32614)
    assert acres == pytest.approx(_KNOWN_ACRES, rel=1e-6)


def test_acres_delta_reports_mismatch(tmp_path: Path) -> None:
    """Declared acreage well below computed area shows up as a positive delta."""
    path = _write(tmp_path, _collection(_feature(acres=200.0)))
    field = load_fields(path)[0]
    assert field.acres_computed == pytest.approx(_KNOWN_ACRES, rel=1e-6)
    assert field.acres_delta_pct == pytest.approx(23.55, abs=0.05)


def test_declared_acres_optional(tmp_path: Path) -> None:
    """A field without 'acres' still loads, with no delta to report."""
    path = _write(tmp_path, _collection(_feature()))
    field = load_fields(path)[0]
    assert field.acres_declared is None
    assert field.acres_delta_pct is None


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("missing", ["id", "name", "crop"])
def test_missing_required_property(tmp_path: Path, missing: str) -> None:
    """Every one of id, name and crop is mandatory."""
    feature = _feature()
    del feature["properties"][missing]
    path = _write(tmp_path, _collection(feature))
    with pytest.raises(FieldValidationError, match=missing):
        load_fields(path)


def test_duplicate_ids_rejected(tmp_path: Path) -> None:
    """Two features sharing an id would collide on the observations primary key."""
    path = _write(tmp_path, _collection(_feature(id="dup"), _feature(id="dup")))
    with pytest.raises(FieldValidationError, match="duplicate field id"):
        load_fields(path)


def test_projected_coordinates_rejected(tmp_path: Path) -> None:
    """UTM metres in a file claiming WGS84 must fail loudly, not silently."""
    utm_square = box(600000, 2899000, 601000, 2900000)
    path = _write(tmp_path, _collection(_feature(geometry=utm_square)))
    with pytest.raises(FieldValidationError, match="outside WGS84 bounds"):
        load_fields(path)


def test_non_polygon_geometry_rejected(tmp_path: Path) -> None:
    """Points and lines have no area, so they cannot be fields."""
    feature = _feature()
    feature["geometry"] = {"type": "Point", "coordinates": [-97.97, 26.20]}
    path = _write(tmp_path, _collection(feature))
    with pytest.raises(FieldValidationError, match="Polygon or MultiPolygon"):
        load_fields(path)


@pytest.mark.parametrize("acres", [-5, 0, "wide"])
def test_bad_acres_rejected(tmp_path: Path, acres: Any) -> None:
    """Declared acreage must be a positive number when present."""
    path = _write(tmp_path, _collection(_feature(acres=acres)))
    with pytest.raises(FieldValidationError, match="acres"):
        load_fields(path)


def test_empty_collection_rejected(tmp_path: Path) -> None:
    """An empty FeatureCollection is a mistake worth naming."""
    path = _write(tmp_path, _collection())
    with pytest.raises(FieldValidationError, match="no features"):
        load_fields(path)


def test_self_intersecting_polygon_repaired(tmp_path: Path) -> None:
    """A bow-tie polygon is repaired rather than rejected, and stays polygonal."""
    bowtie = Polygon(
        [(-97.914, 26.2162), (-97.910, 26.2198), (-97.910, 26.2162), (-97.914, 26.2198)]
    )
    path = _write(tmp_path, _collection(_feature(geometry=bowtie)))
    field = load_fields(path)[0]
    assert field.geometry.is_valid
    assert field.geometry.geom_type in {"Polygon", "MultiPolygon"}
    assert field.acres_computed > 0


def test_geom_hash_tracks_redrawn_polygon(tmp_path: Path) -> None:
    """Moving a single vertex must change the hash that guards the cache."""
    original = load_fields(_write(tmp_path, _collection(_feature())))[0]
    moved = box(
        _KNOWN_SQUARE.bounds[0], _KNOWN_SQUARE.bounds[1],
        _KNOWN_SQUARE.bounds[2] + 0.0002, _KNOWN_SQUARE.bounds[3],
    )
    shifted = load_fields(_write(tmp_path, _collection(_feature(geometry=moved))))[0]
    assert original.geom_hash != shifted.geom_hash


# --------------------------------------------------------------------------- #
# Optional water settings
# --------------------------------------------------------------------------- #


def test_water_settings_accept_plain_words(tmp_path: Path) -> None:
    feature = _feature(irrigation="Rainfed", water_enters="north", soil_awc_in_ft="1.8")
    field = load_fields(_write(tmp_path, _collection(feature)))[0]
    assert (field.irrigation, field.water_enters, field.soil_awc_in_ft) == ("none", "N", 1.8)


def test_water_settings_are_optional(tmp_path: Path) -> None:
    field = load_fields(_write(tmp_path, _collection(_feature())))[0]
    assert (field.irrigation, field.water_enters, field.soil_awc_in_ft) == (None, None, None)


@pytest.mark.parametrize("props, message", [
    ({"irrigation": "bucket"}, "irrigation"),
    ({"water_enters": "uphill"}, "water_enters"),
    ({"soil_awc_in_ft": 9}, "inches per foot"),
])
def test_bad_water_settings_are_rejected(tmp_path: Path, props: dict, message: str) -> None:
    with pytest.raises(FieldValidationError, match=message):
        load_fields(_write(tmp_path, _collection(_feature(**props))))
