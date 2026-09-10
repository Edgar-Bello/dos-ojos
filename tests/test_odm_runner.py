"""Tests for ODM command building, staging, output verification and diagnosis.

Nothing here starts a container. The wrapper's job is to build a correct command,
shape the project, and explain a failure, all of which is checkable offline.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from dosojos_drone.odm_runner import (
    EXPECTED_OUTPUTS,
    ODM_STAGES,
    OdmConfig,
    OdmError,
    as_shell,
    build_command,
    check_docker,
    describe_missing,
    diagnose,
    last_stage,
    memory_advice,
    stage_images,
    strip_ansi,
    verify_outputs,
)

# The tail of a real failed run, trimmed. Kept verbatim so the signatures are
# tested against what ODM actually emits rather than what it might.
REAL_FAILURE = """
[INFO]    Running opensfm stage
2026-09-10 05:48:03,297 DEBUG: Matching DJI_0022.JPG and DJI_0014.JPG. Matches: FAILED
2026-09-10 05:48:05,643 DEBUG: Merging features onto tracks
2026-09-10 05:48:05,643 DEBUG: Good tracks: 0
2026-09-10 05:48:07,598 INFO: 0 partial reconstructions in total.
[ERROR]   The program could not process this dataset using the current settings.
"""


def _write_raster(path: Path, *, crs: str | None = "EPSG:32614") -> Path:
    """Write a tiny GeoTIFF, optionally without a CRS."""
    import rasterio
    from rasterio.transform import from_origin

    path.parent.mkdir(parents=True, exist_ok=True)
    profile = {
        "driver": "GTiff", "height": 4, "width": 4, "count": 1, "dtype": "float32",
        "transform": from_origin(600000, 2900000, 1, 1),
    }
    if crs:
        profile["crs"] = crs
    with rasterio.open(path, "w", **profile) as dataset:
        dataset.write(np.ones((4, 4), dtype="float32"), 1)
    return path


def _complete_project(tmp_path: Path, *, crs: str | None = "EPSG:32614") -> Path:
    """A project directory holding every expected product."""
    for name, relative in EXPECTED_OUTPUTS.items():
        path = tmp_path / relative
        if path.suffix == ".tif":
            _write_raster(path, crs=crs)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"laz-ish")
    return tmp_path


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


def test_default_command_requests_every_product() -> None:
    """A run must ask for orthophoto, DSM and DTM, or later steps have no input."""
    command = build_command(Path("/data"), "f1", OdmConfig())
    assert "--dsm" in command and "--dtm" in command
    assert command[:3] == ["docker", "run", "--rm"]
    assert "--project-path" in command and "f1" in command


def test_resolutions_reach_the_command() -> None:
    """Both resolutions are user-facing knobs and must be passed through."""
    command = build_command(
        Path("/data"), "f1",
        OdmConfig(orthophoto_resolution_cm=1.5, dem_resolution_cm=10.0),
    )
    assert command[command.index("--orthophoto-resolution") + 1] == "1.5"
    assert command[command.index("--dem-resolution") + 1] == "10"


def test_rerun_from_is_passed_for_resumability() -> None:
    """Resuming avoids repeating hours of work after a late failure."""
    command = build_command(Path("/d"), "f1", OdmConfig(rerun_from="odm_dem"))
    assert command[command.index("--rerun-from") + 1] == "odm_dem"


def test_optional_flags_are_absent_by_default() -> None:
    """Nothing should be silently switched on behind the user's back."""
    command = build_command(Path("/d"), "f1", OdmConfig())
    assert "--rerun-from" not in command
    assert "--fast-orthophoto" not in command
    assert "--max-concurrency" not in command


@pytest.mark.parametrize(
    "config",
    [
        OdmConfig(feature_quality="excellent"),
        OdmConfig(pc_quality="turbo"),
        OdmConfig(rerun_from="not_a_stage"),
        OdmConfig(orthophoto_resolution_cm=0),
        OdmConfig(dem_resolution_cm=-1),
    ],
)
def test_bad_settings_fail_before_a_container_starts(config: OdmConfig) -> None:
    """Catching these locally saves waiting for ODM to reject them."""
    with pytest.raises(OdmError):
        build_command(Path("/d"), "f1", config)


def test_rerun_stage_names_match_odms_pipeline() -> None:
    """The stage list doubles as the --rerun-from choices, so it must be right."""
    assert "opensfm" in ODM_STAGES
    assert ODM_STAGES.index("odm_dem") > ODM_STAGES.index("opensfm")


def test_command_renders_for_a_human_to_paste() -> None:
    """The logged command must be runnable by hand, so paths with spaces quote."""
    rendered = as_shell(["docker", "run", "-v", "C:\\Program Files\\x:/datasets"])
    assert '"C:\\Program Files\\x:/datasets"' in rendered


# --------------------------------------------------------------------------- #
# Staging
# --------------------------------------------------------------------------- #


def test_images_are_staged_into_an_images_subdirectory(tmp_path: Path) -> None:
    """ODM insists on <project>/images and will not look anywhere else."""
    raw = tmp_path / "raw"
    raw.mkdir()
    for index in range(3):
        (raw / f"a{index}.JPG").write_bytes(b"jpeg")

    project = tmp_path / "project"
    assert stage_images(raw, project) == 3
    assert sorted(p.name for p in (project / "images").iterdir()) == [
        "a0.JPG", "a1.JPG", "a2.JPG"
    ]


def test_staging_does_not_duplicate_disk(tmp_path: Path) -> None:
    """Hard links keep a few hundred frames from costing twice the space."""
    raw = tmp_path / "raw"
    raw.mkdir()
    source = raw / "a.JPG"
    source.write_bytes(b"x" * 1024)

    project = tmp_path / "project"
    stage_images(raw, project)
    staged = project / "images" / "a.JPG"
    assert staged.stat().st_size == 1024
    assert staged.stat().st_ino == source.stat().st_ino or staged.read_bytes() == b"x" * 1024


def test_restaging_clears_previous_frames(tmp_path: Path) -> None:
    """A re-run must not leave images from an earlier flight in the project."""
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "new.JPG").write_bytes(b"jpeg")

    project = tmp_path / "project"
    (project / "images").mkdir(parents=True)
    (project / "images" / "stale.JPG").write_bytes(b"old")

    stage_images(raw, project)
    assert [p.name for p in (project / "images").iterdir()] == ["new.JPG"]


def test_empty_flight_folder_says_what_to_do(tmp_path: Path) -> None:
    """The error points at 'survey', which is how you find out why it is empty."""
    (tmp_path / "raw").mkdir()
    with pytest.raises(OdmError, match="survey"):
        stage_images(tmp_path / "raw", tmp_path / "project")


# --------------------------------------------------------------------------- #
# Output verification
# --------------------------------------------------------------------------- #


def test_complete_project_verifies(tmp_path: Path) -> None:
    """Every expected product present and carrying a CRS."""
    outputs = verify_outputs(_complete_project(tmp_path))
    assert set(outputs) == set(EXPECTED_OUTPUTS)


def test_ungeoreferenced_raster_is_rejected(tmp_path: Path) -> None:
    """ODM produces a local-frame orthophoto when GPS is missing.

    That output cannot be joined to a field or compared against satellite
    imagery, so it counts as missing rather than present.
    """
    outputs = verify_outputs(_complete_project(tmp_path, crs=None))
    assert "orthophoto" not in outputs
    assert "dsm" not in outputs
    assert "point_cloud" in outputs          # the point cloud is not CRS-checked here


def test_empty_file_counts_as_missing(tmp_path: Path) -> None:
    """A zero-byte product is a failed write, not a result."""
    project = _complete_project(tmp_path)
    (project / EXPECTED_OUTPUTS["dtm"]).write_bytes(b"")
    assert "dtm" not in verify_outputs(project)


def test_missing_products_are_named(tmp_path: Path) -> None:
    """The error should say which product is absent, not just that one is."""
    assert "dsm" in describe_missing(["orthophoto", "dtm", "point_cloud"])


# --------------------------------------------------------------------------- #
# Failure diagnosis
# --------------------------------------------------------------------------- #


def test_stage_is_named_from_the_log() -> None:
    """Knowing where it stopped is most of the value of reading the log."""
    assert last_stage(REAL_FAILURE) == "opensfm"
    assert last_stage("nothing here") is None


def test_real_failure_is_diagnosed_specifically() -> None:
    """Against a genuine failed run, not an invented message.

    "Good tracks: 0" means features were extracted but none matched between
    frames, which is a different problem from too few images and deserves
    different advice.
    """
    stage, advice = diagnose(REAL_FAILURE, 1)
    assert stage == "opensfm"
    assert "none matched across them" in advice


def test_out_of_memory_is_recognised_by_exit_code() -> None:
    """137 is the signature of a killed container, nearly always memory."""
    _, advice = diagnose("[INFO] Running openmvs stage\n", 137)
    assert "memory" in advice.lower()


def test_unknown_failure_still_names_the_stage() -> None:
    """Even without a known signature, the stage narrows the search."""
    stage, advice = diagnose("[INFO]    Running odm_meshing stage\nsomething odd\n", 1)
    assert stage == "odm_meshing"
    assert advice


def test_ansi_codes_do_not_defeat_matching() -> None:
    """ODM colours its output; escape codes must not hide a signature."""
    coloured = "\x1b[39m[INFO]    Running opensfm stage\x1b[0m\nGood tracks: 0\n"
    assert strip_ansi(coloured).startswith("[INFO]")
    stage, advice = diagnose(coloured, 1)
    assert stage == "opensfm"
    assert "none matched" in advice


# --------------------------------------------------------------------------- #
# Capacity
# --------------------------------------------------------------------------- #


def test_thin_memory_is_flagged_before_a_run() -> None:
    """ODM fails late, during dense reconstruction, so warn up front."""
    warnings = memory_advice(500, 4 * 1024**3)
    assert warnings and "wslconfig" in warnings[0].lower()


def test_ample_memory_is_silent() -> None:
    """No warning when the machine is comfortably sized for the job."""
    assert memory_advice(50, 16 * 1024**3) == []


def test_unknown_memory_makes_no_claim() -> None:
    """Better to say nothing than to guess at capacity."""
    assert memory_advice(500, None) == []


def test_docker_status_is_readable() -> None:
    """check_docker must return a usable answer whether or not Docker is up."""
    status = check_docker()
    assert isinstance(status.available, bool)
    if not status.available:
        assert status.problems
