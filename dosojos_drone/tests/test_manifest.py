"""Tests for flights.json, the only thing joining a flight to a satellite field."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from dosojos_drone.config import (
    Flight,
    ManifestError,
    Settings,
    get_flight,
    load_manifest,
    register_flight,
    save_manifest,
)


def test_missing_manifest_reads_as_empty(tmp_path: Path) -> None:
    """A fresh project has no manifest yet; that is not an error."""
    assert load_manifest(tmp_path / "flights.json") == {}


def test_round_trip_preserves_every_field(tmp_path: Path) -> None:
    """What goes into the manifest comes back out unchanged."""
    path = tmp_path / "flights.json"
    flight = Flight(
        flight_id="demo-001", field_id="rgv-002", flown_on=date(2026, 9, 5),
        crop="grain sorghum", notes="hazy", ground_elevation_m=12.0,
    )
    save_manifest(path, {"demo-001": flight})
    assert load_manifest(path)["demo-001"] == flight


def test_field_id_is_required(tmp_path: Path) -> None:
    """Without a field_id the flight cannot join the satellite results at all."""
    path = tmp_path / "flights.json"
    path.write_text(json.dumps({"flights": {"f1": {"crop": "cane"}}}), encoding="utf-8")
    with pytest.raises(ManifestError, match="field_id"):
        load_manifest(path)


def test_malformed_manifest_names_the_file(tmp_path: Path) -> None:
    """A broken manifest fails with the path, not a bare JSON error."""
    path = tmp_path / "flights.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ManifestError, match="not valid JSON"):
        load_manifest(path)

    path.write_text(json.dumps({"nope": {}}), encoding="utf-8")
    with pytest.raises(ManifestError, match="'flights' key"):
        load_manifest(path)


def test_unknown_flight_says_how_to_register_it(tmp_path: Path) -> None:
    """The error is the instruction, so nobody has to go looking for it."""
    path = tmp_path / "flights.json"
    save_manifest(path, {"a": Flight("a", "rgv-001")})
    with pytest.raises(ManifestError, match="register"):
        get_flight(path, "missing")


def test_remapping_a_flight_needs_force(tmp_path: Path) -> None:
    """Silently repointing a flight at another field would corrupt the join."""
    path = tmp_path / "flights.json"
    register_flight(path, Flight("demo-001", "rgv-002"))

    with pytest.raises(ManifestError, match="already mapped"):
        register_flight(path, Flight("demo-001", "rgv-003"))

    updated = register_flight(path, Flight("demo-001", "rgv-003"), overwrite=True)
    assert updated.field_id == "rgv-003"
    assert load_manifest(path)["demo-001"].field_id == "rgv-003"


def test_re_registering_the_same_mapping_is_harmless(tmp_path: Path) -> None:
    """Running register twice with the same arguments must not fail."""
    path = tmp_path / "flights.json"
    register_flight(path, Flight("demo-001", "rgv-002"))
    assert register_flight(path, Flight("demo-001", "rgv-002")).field_id == "rgv-002"


def test_manifest_is_written_in_id_order(tmp_path: Path) -> None:
    """Stable ordering keeps the diff readable when flights are added."""
    path = tmp_path / "flights.json"
    save_manifest(path, {
        "c": Flight("c", "f3"), "a": Flight("a", "f1"), "b": Flight("b", "f2"),
    })
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert list(payload["flights"]) == ["a", "b", "c"]


def test_settings_locate_the_satellite_fields(tmp_path: Path) -> None:
    """The drone project reads the satellite polygons; a path, not an import."""
    settings = Settings.from_root(tmp_path)
    assert settings.fields_geojson.name == "fields.geojson"
    assert "dosojos_sat" in str(settings.fields_geojson)
    assert settings.flight_raw("f1") == tmp_path / "data" / "raw" / "f1"
    assert settings.flight_out("f1") == tmp_path / "out" / "f1"


def test_source_and_row_spacing_survive_the_manifest(tmp_path: Path) -> None:
    """Both are set once at registration and read by later steps."""
    path = tmp_path / "flights.json"
    save_manifest(path, {"p": Flight(
        flight_id="p", field_id="PUBLIC-x", source="Purdue University, CC0",
        row_spacing_m=0.762,
    )})
    flight = load_manifest(path)["p"]
    assert flight.source == "Purdue University, CC0"
    assert flight.row_spacing_m == 0.762


def test_a_workspace_keeps_its_satellite_link_relative(tmp_path: Path) -> None:
    """A demo workspace laid out like the project pairs with its own satellite half."""
    workspace = tmp_path / "demo" / "dosojos_drone"
    settings = Settings.from_root(workspace)
    assert settings.fields_geojson == (tmp_path / "demo" / "dosojos_sat" / "fields.geojson").resolve()
    assert settings.satellite_flags == (tmp_path / "demo" / "dosojos_sat" / "out" / "flags.json").resolve()
