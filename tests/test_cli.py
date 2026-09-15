"""Tests for how the command line is reached."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from click.testing import CliRunner

from dosojos_drone import odm_runner
from dosojos_drone.cli import cli

UTF8 = {"encoding": "utf-8", "errors": "replace",
        "env": {**os.environ, "PYTHONIOENCODING": "utf-8"}}


@pytest.mark.parametrize("module", ["dosojos_drone", "dosojos_drone.cli"])
def test_python_dash_m_reaches_every_command(module: str) -> None:
    """The way in when Windows blocks the .exe launcher must expose the whole CLI."""
    result = subprocess.run([sys.executable, "-m", module, "--help"],
                            capture_output=True, check=False, **UTF8)
    assert result.returncode == 0, result.stderr
    listed = {line.split()[0] for line in result.stdout.split("Commands:")[1].splitlines()
              if line.strip()}
    assert set(cli.commands) <= listed


def test_python_dash_m_ignores_a_folder_named_like_the_package(tmp_path: Path) -> None:
    """Dos_Ojos/ and every demo hold a dosojos_drone folder, which must not hide the package."""
    (tmp_path / "dosojos_drone" / "config").mkdir(parents=True)
    result = subprocess.run([sys.executable, "-m", "dosojos_drone", "--help"],
                            cwd=tmp_path, capture_output=True, check=False, **UTF8)
    assert result.returncode == 0, result.stderr
    assert "detect" in result.stdout


@pytest.mark.parametrize("status, code", [
    (odm_runner.DockerStatus(available=False, problems=["the Docker daemon is not responding"]), 1),
    (odm_runner.DockerStatus(available=True, version="27.0", has_image=False), 1),
    (odm_runner.DockerStatus(available=True, version="27.0", has_image=True), 0),
])
def test_doctor_fails_a_script_until_odm_can_run(monkeypatch, status, code: int) -> None:
    monkeypatch.setattr(odm_runner, "check_docker", lambda: status)
    result = CliRunner().invoke(cli, ["doctor"])
    assert result.exit_code == code, result.output
    assert ("Ready to run ODM." in result.output) == (code == 0)
