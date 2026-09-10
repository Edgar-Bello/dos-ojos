"""Paths, settings, and the flight manifest that ties a flight to a field.

A flight folder is named by ``flight_id``; every output has to join the satellite
pipeline on ``field_id``. ``flights.json`` is the mapping between them, plus the
flight metadata that is worth recording but does not live in EXIF.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MANIFEST_NAME = "flights.json"

#: Where the satellite half keeps its field polygons. Read-only, and the join
#: between the two halves is this file plus a shared field_id, nothing more.
SATELLITE_FIELDS = Path("../dosojos_sat/fields.geojson")


class ManifestError(RuntimeError):
    """Raised when the flight manifest is missing, malformed, or lacks a flight."""


@dataclass(frozen=True)
class Flight:
    """One flight's identity and its link to a satellite field."""

    flight_id: str
    field_id: str
    flown_on: date | None = None
    crop: str | None = None
    notes: str | None = None
    ground_elevation_m: float | None = None

    @classmethod
    def from_dict(cls, flight_id: str, payload: dict[str, Any]) -> "Flight":
        """Build a flight from one manifest entry."""
        if not isinstance(payload, dict):
            raise ManifestError(f"flight {flight_id!r}: entry must be an object")
        field_id = payload.get("field_id")
        if not field_id:
            raise ManifestError(
                f"flight {flight_id!r}: 'field_id' is required, since it is the only "
                "thing joining this flight to the satellite results"
            )
        flown = payload.get("flown_on")
        return cls(
            flight_id=flight_id,
            field_id=str(field_id),
            flown_on=date.fromisoformat(flown) if flown else None,
            crop=payload.get("crop"),
            notes=payload.get("notes"),
            ground_elevation_m=payload.get("ground_elevation_m"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Render back to a manifest entry, omitting empty fields."""
        payload: dict[str, Any] = {"field_id": self.field_id}
        if self.flown_on:
            payload["flown_on"] = self.flown_on.isoformat()
        for key in ("crop", "notes", "ground_elevation_m"):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        return payload


@dataclass(frozen=True)
class Settings:
    """Resolved paths for one CLI invocation."""

    root: Path
    raw_dir: Path
    odm_dir: Path
    out_dir: Path
    manifest_path: Path
    fields_geojson: Path

    @classmethod
    def from_root(cls, root: Path, **overrides: object) -> "Settings":
        """Build settings rooted at ``root``, ignoring ``None`` overrides."""
        base = cls(
            root=root,
            raw_dir=root / "data" / "raw",
            odm_dir=root / "data" / "odm",
            out_dir=root / "out",
            manifest_path=root / MANIFEST_NAME,
            fields_geojson=(root / SATELLITE_FIELDS).resolve(),
        )
        supplied = {k: v for k, v in overrides.items() if v is not None}
        return replace(base, **supplied) if supplied else base

    def flight_raw(self, flight_id: str) -> Path:
        """Folder holding one flight's source images."""
        return self.raw_dir / flight_id

    def flight_odm(self, flight_id: str) -> Path:
        """Folder ODM writes its products into."""
        return self.odm_dir / flight_id

    def flight_out(self, flight_id: str) -> Path:
        """Folder for this pipeline's own outputs."""
        return self.out_dir / flight_id

    def ensure_dirs(self, flight_id: str | None = None) -> None:
        """Create the shared directories, and one flight's if named."""
        for path in (self.raw_dir, self.odm_dir, self.out_dir):
            path.mkdir(parents=True, exist_ok=True)
        if flight_id:
            self.flight_out(flight_id).mkdir(parents=True, exist_ok=True)


def default_root() -> Path:
    """Project root, the directory holding ``src/``, ``data/`` and ``out/``."""
    return Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #


def load_manifest(path: Path) -> dict[str, Flight]:
    """Read ``flights.json``, returning an empty mapping if it does not exist yet."""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManifestError(f"{path}: not valid JSON ({exc})") from exc

    entries = payload.get("flights") if isinstance(payload, dict) else None
    if not isinstance(entries, dict):
        raise ManifestError(f"{path}: expected an object with a 'flights' key")
    return {
        flight_id: Flight.from_dict(flight_id, entry)
        for flight_id, entry in entries.items()
    }


def save_manifest(path: Path, flights: dict[str, Flight]) -> Path:
    """Write the manifest, keeping flights in id order for a readable diff."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "flights": {
            flight_id: flights[flight_id].to_dict() for flight_id in sorted(flights)
        }
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def get_flight(path: Path, flight_id: str) -> Flight:
    """Look up one flight, with a message that says how to register it.

    Raises:
        ManifestError: if the manifest or the flight is missing.
    """
    flights = load_manifest(path)
    if flight_id not in flights:
        known = ", ".join(sorted(flights)) or "none registered"
        raise ManifestError(
            f"flight {flight_id!r} is not in {path.name} (known: {known}). "
            f"Register it with: dosojos-drone register {flight_id} --field <field_id>"
        )
    return flights[flight_id]


def register_flight(path: Path, flight: Flight, *, overwrite: bool = False) -> Flight:
    """Add or update one flight in the manifest.

    Raises:
        ManifestError: if the flight exists and ``overwrite`` was not requested.
    """
    flights = load_manifest(path)
    if flight.flight_id in flights and not overwrite:
        existing = flights[flight.flight_id]
        if existing.field_id != flight.field_id:
            raise ManifestError(
                f"flight {flight.flight_id!r} is already mapped to field "
                f"{existing.field_id!r}; pass --force to remap it to "
                f"{flight.field_id!r}"
            )
        return existing
    flights[flight.flight_id] = flight
    save_manifest(path, flights)
    log.info("registered %s -> field %s", flight.flight_id, flight.field_id)
    return flight
