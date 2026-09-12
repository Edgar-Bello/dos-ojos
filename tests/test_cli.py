"""Tests for command-line options that keep demo data apart and judge past dates."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
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


@pytest.mark.parametrize("module", ["dosojos_sat", "dosojos_sat.cli"])
def test_python_dash_m_reaches_every_command(module: str) -> None:
    """The way in when Windows blocks the .exe launcher must expose the whole CLI."""
    result = subprocess.run(
        [sys.executable, "-m", module, "--help"], capture_output=True, check=False,
        encoding="utf-8", errors="replace", env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert result.returncode == 0, result.stderr
    listed = {line.split()[0] for line in result.stdout.split("Commands:")[1].splitlines()
              if line.strip()}
    assert set(cli.commands) <= listed


def test_the_water_command_ranks_fields_and_draws_each_one(tmp_path: Path) -> None:
    """From a cached season, a field log and a soil, to water.json, a chart and a CSV."""
    from datetime import timedelta

    from dosojos_sat import cache
    from dosojos_sat.indices import IndexStats
    from dosojos_sat.soils import manual_profile

    feature = json.loads(json.dumps(FIELD))
    feature["features"][0]["properties"]["irrigation"] = "furrow"
    fields = tmp_path / "fields.geojson"
    fields.write_text(json.dumps(feature), encoding="utf-8")
    workspace = tmp_path / "demo"
    runner = CliRunner()
    assert runner.invoke(cli, ["--workspace", str(workspace), "init-fields", str(fields)]).exit_code == 0

    start, as_of = date(2026, 4, 1), date(2026, 6, 10)
    with cache.session(workspace / "cache" / "dosojos.sqlite") as conn:
        field = cache.get_fields(conn)[0]
        for offset in range(0, 70, 5):
            median = min(0.85, 0.2 + offset / 80)
            stats = IndexStats(mean=median, median=median, p10=median, p25=median, p75=median,
                               p90=median, std=0.01, valid_fraction=1.0, n_valid_px=9,
                               n_total_px=9)
            cache.upsert_observation(conn, cache.ObservationRecord(
                field_id=field.field_id, obs_date=start + timedelta(days=offset),
                index_name="NDVI", stats=stats, scene_id="S2"))
        days = [start + timedelta(days=i) for i in range((as_of - start).days + 1)]
        cache.upsert_weather(conn, field.field_id, pd.DataFrame(
            {"date": days, "eto_mm": 6.0, "rain_mm": 0.0}), "gridmet")
        cache.upsert_soil(conn, field.field_id, field.geom_hash, "manual",
                          manual_profile(1.6).to_dict())
    (workspace / "field_log.csv").write_text(
        "field_id,date,event,inches,notes\n"
        "PUBLIC-test,2026-04-01,planted,,\nPUBLIC-test,2026-06-01,irrigated,,\n",
        encoding="utf-8")

    result = runner.invoke(cli, ["--workspace", str(workspace), "water", "--as-of", "2026-06-10"])

    assert result.exit_code == 0, result.output
    payload = json.loads((workspace / "out" / "water.json").read_text(encoding="utf-8"))
    entry = payload["fields"][0]
    assert entry["field_id"] == "PUBLIC-test" and entry["rank"] == 1
    assert entry["days_left"] is not None and entry["method"] == "furrow"
    assert (workspace / "out" / "PUBLIC-test_water.png").exists()
    assert (workspace / "out" / "water" / "PUBLIC-test_daily.csv").exists()
    assert "who needs water first" in result.output


def test_the_water_command_needs_a_soil_first(tmp_path: Path) -> None:
    fields = tmp_path / "fields.geojson"
    fields.write_text(json.dumps(FIELD), encoding="utf-8")
    workspace = tmp_path / "demo"
    runner = CliRunner()
    runner.invoke(cli, ["--workspace", str(workspace), "init-fields", str(fields)])
    result = runner.invoke(cli, ["--workspace", str(workspace), "water"])
    assert result.exit_code != 0
    assert "dosojos-sat soil" in result.output


def test_python_dash_m_ignores_a_folder_named_like_the_package(tmp_path: Path) -> None:
    """Dos_Ojos/ and every demo hold a dosojos_sat folder, which must not hide the package."""
    (tmp_path / "dosojos_sat" / "config").mkdir(parents=True)
    result = subprocess.run(
        [sys.executable, "-m", "dosojos_sat", "--help"], cwd=tmp_path, capture_output=True,
        check=False, encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert result.returncode == 0, result.stderr
    assert "init-fields" in result.stdout
