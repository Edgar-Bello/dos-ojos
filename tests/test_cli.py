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
