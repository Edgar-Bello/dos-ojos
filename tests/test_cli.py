"""Tests for command-line options that keep demo data apart and judge past dates."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
from click.testing import CliRunner

from dosojos_sat.cli import _until, cli

FIELD = {
    "type": "FeatureCollection",
    "features": [{
        "type": "Feature",
        "properties": {"id": "PUBLIC-test", "name": "FREE DATA test", "crop": "sorghum"},
        "geometry": {"type": "Polygon", "coordinates": [[
            [-86.990, 40.476], [-86.989, 40.476], [-86.989, 40.479],
            [-86.990, 40.479], [-86.990, 40.476],
        ]]},
    }],
}


def test_a_workspace_keeps_its_cache_apart(tmp_path: Path) -> None:
    """Public demo fields must never land in the cache holding the real ones."""
    fields = tmp_path / "fields.geojson"
    fields.write_text(json.dumps(FIELD), encoding="utf-8")
    workspace = tmp_path / "demo"

    result = CliRunner().invoke(cli, ["--workspace", str(workspace), "init-fields", str(fields)])

    assert result.exit_code == 0, result.output
    assert (workspace / "cache" / "dosojos.sqlite").exists()
    assert "PUBLIC-test" in result.output


def test_observations_after_the_judged_date_are_dropped() -> None:
    """Judged as of a flight, the season may only use what was known that day."""
    frame = pd.DataFrame({"date": [date(2018, 7, 1), date(2018, 7, 10), date(2018, 8, 1)],
                          "median": [0.8, 0.85, 0.7]})
    kept = _until(frame, date(2018, 7, 10))
    assert list(kept["date"]) == [date(2018, 7, 1), date(2018, 7, 10)]
    assert _until(frame, None) is frame
