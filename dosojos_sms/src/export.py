"""Writing what farmers told us into the files the two halves already read.

- ``dosojos_sat/fields.geojson``: every field with an outline and a crop, with
  its irrigation method and the side the water comes in from.
- ``dosojos_sat/field_log.csv``: plantings, irrigations, rain gauge readings and
  harvests, each with the text message it came from in ``notes``.
- ``dosojos_drone/flights.json``: a flight for every finished photo upload.

Only confirmed facts get here: an event exists only once the farmer said yes to
its read-back, and a voided event is left out.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import sqlite3
from dataclasses import dataclass, field as dc_field
from pathlib import Path

from dosojos_sat.fields import FieldValidationError, load_fields

from . import store
from .config import Settings
from .status import LOG_KINDS, crop_text
from .store import FieldRow

log = logging.getLogger(__name__)


@dataclass
class Exported:
    fields_path: Path
    log_path: Path
    field_ids: list[str] = dc_field(default_factory=list)
    events: int = 0
    skipped: list[tuple[str, str]] = dc_field(default_factory=list)


def _write_atomic(path: Path, content: str) -> None:
    """Write beside, then swap in, so a half-written file is never read."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="")
    os.replace(temporary, path)


def feature(record: FieldRow) -> dict:
    """One field as the satellite half's fields.geojson wants it."""
    properties: dict = {"id": record.id, "name": record.name, "crop": crop_text(record)}
    if record.acres_said:
        properties["acres"] = record.acres_said
    if record.irrigation:
        properties["irrigation"] = record.irrigation
    if record.water_enters:
        properties["water_enters"] = record.water_enters
    return {"type": "Feature", "properties": properties, "geometry": record.outline}


def export(conn: sqlite3.Connection, settings: Settings) -> Exported:
    """Write fields.geojson and field_log.csv into the farm's satellite workspace.

    Raises:
        FieldValidationError: if an outline the satellite half would refuse got in,
            naming the field, so it can be redrawn.
    """
    workspace = settings.sat_workspace
    result = Exported(workspace / "fields.geojson", workspace / "field_log.csv")
    features = []
    for record in store.all_fields(conn):
        if record.outline is None:
            result.skipped.append((record.id, "no map yet"))
        elif not record.crop:
            result.skipped.append((record.id, "no crop yet"))
        else:
            features.append(feature(record))
            result.field_ids.append(record.id)
    if features:
        content = json.dumps({"type": "FeatureCollection", "features": features}, indent=2)
        _write_atomic(result.fields_path, content + "\n")
        load_fields(result.fields_path)          # the satellite half's own check, now
    elif result.fields_path.exists():
        result.fields_path.unlink()

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["field_id", "date", "event", "inches", "notes"])
    exported = set(result.field_ids)
    for event in store.events_for(conn):
        if event.field_id not in exported or event.kind not in LOG_KINDS:
            continue
        notes = [f"sms message {event.message_id}" if event.message_id else event.source]
        if event.evidence:
            notes.append(f"photo {Path(event.evidence).name}")
        if event.note:
            notes.append(event.note)
        writer.writerow([event.field_id, event.day.isoformat(), event.kind,
                         "" if event.inches is None else f"{event.inches:g}", "; ".join(notes)])
        result.events += 1
    _write_atomic(result.log_path, buffer.getvalue())
    return result


def register_flight(settings: Settings, upload: sqlite3.Row, record: FieldRow) -> str:
    """Add a finished upload to the drone workspace's flights.json, as the drone CLI does."""
    manifest = settings.drone_workspace / "flights.json"
    payload = {"flights": {}}
    if manifest.exists():
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    flights = payload.setdefault("flights", {})
    notes = [f"uploaded by text message from {record.name}"]
    if upload["bare_soil"] == 1:
        notes.append("bare soil, no crop: use for the ground map")
    elif upload["bare_soil"] == 0:
        notes.append("crop standing")
    entry = {"field_id": record.id, "notes": "; ".join(notes)}
    if upload["flown_on"]:
        entry["flown_on"] = upload["flown_on"]
    if record.crop and record.crop not in ("none", "other"):
        entry["crop"] = crop_text(record)
    flights.setdefault(upload["flight_id"], entry)
    payload["flights"] = {k: flights[k] for k in sorted(flights)}
    _write_atomic(manifest, json.dumps(payload, indent=2) + "\n")
    return upload["flight_id"]


__all__ = ["Exported", "FieldValidationError", "export", "feature", "register_flight"]
