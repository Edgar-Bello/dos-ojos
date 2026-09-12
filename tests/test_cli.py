"""Tests for how the command line is reached."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

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
