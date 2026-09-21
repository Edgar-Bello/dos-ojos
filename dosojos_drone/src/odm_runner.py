"""OpenDroneMap invocation: stage images, run the container, verify what came out.

Photogrammetry is not reimplemented here and should not be. This module's job is
to hand ODM a correctly shaped project, record the exact command so a run can be
reproduced by hand, and turn a failure into a sentence naming the stage that
broke rather than a forty thousand line log.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Iterable, Sequence

log = logging.getLogger(__name__)

ODM_IMAGE = "opendronemap/odm:latest"

#: Products a run must yield before the next step is worth attempting.
EXPECTED_OUTPUTS: dict[str, str] = {
    "orthophoto": "odm_orthophoto/odm_orthophoto.tif",
    "dsm": "odm_dem/dsm.tif",
    "dtm": "odm_dem/dtm.tif",
    "point_cloud": "odm_georeferencing/odm_georeferenced_model.laz",
}

#: ODM's pipeline, in order. Used to resolve --rerun-from and to name failures.
ODM_STAGES: tuple[str, ...] = (
    "dataset", "split", "merge", "opensfm", "openmvs", "odm_filterpoints",
    "odm_meshing", "mvs_texturing", "odm_georeferencing", "odm_dem",
    "odm_orthophoto", "odm_report", "odm_postprocess",
)

#: Rough working memory ODM wants, by image count, at medium quality. Derived
#: from the project's own sizing guidance; generous settings need more.
MEMORY_GUIDANCE_GB: tuple[tuple[int, int], ...] = (
    (100, 4), (250, 8), (500, 16), (1500, 32), (2500, 64),
)

IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".tif", ".tiff"})


class OdmError(RuntimeError):
    """Raised when a run cannot start, or its outputs are unusable."""


# --------------------------------------------------------------------------- #
# Docker
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DockerStatus:
    """Whether this machine can run ODM, and on how many images."""

    available: bool
    version: str | None = None
    memory_bytes: int | None = None
    cpus: int | None = None
    has_image: bool = False
    problems: list[str] = dc_field(default_factory=list)

    @property
    def memory_gb(self) -> float | None:
        """Memory available to containers, in gigabytes."""
        return self.memory_bytes / 1024**3 if self.memory_bytes else None


def check_docker(image: str = ODM_IMAGE) -> DockerStatus:
    """Report whether Docker is usable and whether the ODM image is present."""
    if shutil.which("docker") is None:
        return DockerStatus(
            available=False,
            problems=[
                "docker is not on PATH. Install Docker Desktop, then reopen this "
                "terminal so it picks up the new PATH."
            ],
        )

    probe = subprocess.run(
        ["docker", "info", "--format",
         "{{.ServerVersion}}\t{{.MemTotal}}\t{{.NCPU}}"],
        capture_output=True, text=True, check=False,
    )
    if probe.returncode != 0:
        return DockerStatus(
            available=False,
            problems=[
                "the Docker daemon is not responding. Start Docker Desktop and "
                f"wait for it to report Running. ({probe.stderr.strip()[:200]})"
            ],
        )

    version, memory, cpus = (probe.stdout.strip().split("\t") + ["", "", ""])[:3]
    listed = subprocess.run(
        ["docker", "images", "-q", image], capture_output=True, text=True, check=False
    )
    return DockerStatus(
        available=True,
        version=version or None,
        memory_bytes=int(memory) if memory.isdigit() else None,
        cpus=int(cpus) if cpus.isdigit() else None,
        has_image=bool(listed.stdout.strip()),
        problems=[],
    )


def memory_advice(n_images: int, memory_bytes: int | None) -> list[str]:
    """Warn when the container memory looks thin for this many images.

    Being wrong in the optimistic direction costs hours, since ODM tends to fail
    late, during dense reconstruction, rather than refusing up front.
    """
    if not memory_bytes:
        return []
    available_gb = memory_bytes / 1024**3
    wanted_gb = next(
        (gb for limit, gb in MEMORY_GUIDANCE_GB if n_images <= limit),
        MEMORY_GUIDANCE_GB[-1][1],
    )
    if available_gb >= wanted_gb:
        return []
    return [
        f"Docker has {available_gb:.1f} GB but {n_images} images usually want "
        f"about {wanted_gb} GB. Expect a slow run, and a possible out-of-memory "
        "failure during dense reconstruction. Raise memory in %USERPROFILE%\\"
        ".wslconfig, add swap, or lower --pc-quality."
    ]


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class OdmConfig:
    """ODM settings tuned for a small, flat agricultural field."""

    orthophoto_resolution_cm: float = 2.0
    dem_resolution_cm: float = 5.0
    feature_quality: str = "medium"
    pc_quality: str = "medium"
    max_concurrency: int | None = None
    rerun_from: str | None = None
    fast_orthophoto: bool = False
    extra_args: tuple[str, ...] = ()

    def validate(self) -> None:
        """Reject settings ODM would refuse, before a container is started."""
        allowed = {"ultra", "high", "medium", "low", "lowest"}
        for name, value in (
            ("feature-quality", self.feature_quality),
            ("pc-quality", self.pc_quality),
        ):
            if value not in allowed:
                raise OdmError(
                    f"--{name} must be one of {', '.join(sorted(allowed))}, "
                    f"got {value!r}"
                )
        if self.rerun_from and self.rerun_from not in ODM_STAGES:
            raise OdmError(
                f"--rerun-from must name an ODM stage: {', '.join(ODM_STAGES)}"
            )
        if self.orthophoto_resolution_cm <= 0 or self.dem_resolution_cm <= 0:
            raise OdmError("resolutions must be positive, in centimetres per pixel")


@dataclass(frozen=True)
class OdmResult:
    """What a run did, whether or not it succeeded."""

    flight_id: str
    project_dir: Path
    command: list[str]
    returncode: int
    duration_s: float
    log_path: Path
    outputs: dict[str, Path]
    failed_stage: str | None = None
    advice: str = ""

    @property
    def ok(self) -> bool:
        """True when ODM exited cleanly and every expected product exists."""
        return self.returncode == 0 and len(self.outputs) == len(EXPECTED_OUTPUTS)


# --------------------------------------------------------------------------- #
# Staging
# --------------------------------------------------------------------------- #


def stage_images(raw_dir: Path, project_dir: Path) -> int:
    """Populate ``<project>/images`` from a flight folder, returning the count.

    ODM insists its images sit in an ``images`` subdirectory of the project. Files
    are hard-linked rather than copied so a few hundred frames cost no extra disk,
    falling back to a copy when the two paths are on different volumes.

    Raises:
        OdmError: if the flight folder holds no images.
    """
    raw_dir, project_dir = Path(raw_dir), Path(project_dir)
    sources = sorted(
        p for p in raw_dir.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
    ) if raw_dir.is_dir() else []
    if not sources:
        raise OdmError(
            f"no images in {raw_dir}. Run 'dosojos-drone survey <flight_id>' first "
            "to confirm the folder is what you expect."
        )

    images_dir = project_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    for stale in images_dir.iterdir():
        if stale.is_file():
            stale.unlink()

    for source in sources:
        target = images_dir / source.name
        try:
            os.link(source, target)
        except OSError:
            shutil.copy2(source, target)

    log.info("staged %d image(s) into %s", len(sources), images_dir)
    return len(sources)


# --------------------------------------------------------------------------- #
# Command
# --------------------------------------------------------------------------- #


def build_command(
    datasets_dir: Path, flight_id: str, config: OdmConfig, *, image: str = ODM_IMAGE
) -> list[str]:
    """Assemble the full docker command for one run.

    Kept separate from running it so the exact invocation can be logged, tested,
    and pasted into a terminal unchanged.
    """
    config.validate()
    command = [
        "docker", "run", "--rm",
        "-v", f"{Path(datasets_dir).resolve()}:/datasets",
        image,
        "--project-path", "/datasets", flight_id,
        "--orthophoto-resolution", f"{config.orthophoto_resolution_cm:g}",
        "--dem-resolution", f"{config.dem_resolution_cm:g}",
        "--feature-quality", config.feature_quality,
        "--pc-quality", config.pc_quality,
        "--dsm", "--dtm",
        "--time",
    ]
    if config.max_concurrency:
        command += ["--max-concurrency", str(config.max_concurrency)]
    if config.fast_orthophoto:
        command.append("--fast-orthophoto")
    if config.rerun_from:
        command += ["--rerun-from", config.rerun_from]
    command += list(config.extra_args)
    return command


def as_shell(command: Sequence[str]) -> str:
    """Render a command for a human to paste, quoting only what needs it."""
    return " ".join(f'"{part}"' if " " in part else part for part in command)


# --------------------------------------------------------------------------- #
# Failure diagnosis
# --------------------------------------------------------------------------- #

_STAGE_LINE = re.compile(r"Running (\w+) stage")
#: ODM colours its output, and the escape codes make logs hard to read and
#: signature matching fragile.
_ANSI = re.compile(r"\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    """Remove terminal colour codes from ODM output."""
    return _ANSI.sub("", text)

#: Signatures worth recognising, with what to do about each. Ordered, so the
#: most specific match wins.
_SIGNATURES: tuple[tuple[str, str], ...] = (
    (
        "Good tracks: 0",
        "Features were found in individual images but none matched across them, "
        "so nothing could be triangulated. That means either too little overlap, "
        "images from more than one area, heavy motion blur, or a surface too "
        "self-similar to match (bare soil, water, or uniform canopy). Check the "
        "overlap figure from 'survey' first.",
    ),
    (
        "0 partial reconstructions in total",
        "Feature matching produced no reconstruction at all. Confirm with "
        "'survey' that overlap is above 70% and that every frame is from the "
        "same flight over the same field.",
    ),
    (
        "could not process this dataset using the current settings",
        "ODM gave up on the dataset. Its own advice is to check overlap, "
        "recognisable features and focus. Try --feature-quality high, which "
        "extracts more features per image, before assuming the flight is bad.",
    ),
    (
        "not enough overlap",
        "Images do not overlap enough to reconstruct. Re-fly with at least 70% "
        "forward and 65% side overlap, or extract video frames at a higher rate.",
    ),
    (
        "Not enough supported images",
        "Too few images could be matched. Check they are all from one flight over "
        "one area, and that 'survey' reported a healthy overlap.",
    ),
    (
        "No images found",
        "ODM saw an empty images directory. The staging step should have filled "
        "<project>/images; check the flight folder is not empty.",
    ),
    (
        "MemoryError",
        "Ran out of memory. Raise memory in %USERPROFILE%\\.wslconfig, add swap, "
        "or drop --pc-quality to low.",
    ),
    (
        "Killed",
        "The process was killed, which almost always means out of memory. Raise "
        "memory in %USERPROFILE%\\.wslconfig, add swap, or lower --pc-quality.",
    ),
    (
        "Cannot find a valid georeference",
        "Nothing could be georeferenced. The images likely lack GPS; 'survey' "
        "reports this before a run is worth starting.",
    ),
    (
        "cannot allocate memory",
        "The container could not allocate memory. Raise the WSL memory limit or "
        "close other applications before running.",
    ),
)


def last_stage(log_text: str) -> str | None:
    """Name the last ODM stage that started, which is where a failure happened."""
    matches = _STAGE_LINE.findall(log_text)
    return matches[-1] if matches else None


def diagnose(log_text: str, returncode: int) -> tuple[str | None, str]:
    """Turn a failed run into the stage that broke and what to do about it."""
    log_text = strip_ansi(log_text)
    stage = last_stage(log_text)
    haystack = log_text.lower()
    for signature, remedy in _SIGNATURES:
        if signature.lower() in haystack:
            return stage, remedy
    if returncode == 137:
        return stage, (
            "Exit code 137 means the container was killed, nearly always out of "
            "memory. Raise memory in %USERPROFILE%\\.wslconfig or add swap."
        )
    return stage, (
        "No known failure signature was found. The tail of the log is the place "
        "to look; the stage named above is where it stopped."
    )


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #


def verify_outputs(project_dir: Path) -> dict[str, Path]:
    """Return the products that exist, are non-empty, and carry a CRS.

    A georeference check matters because ODM will happily produce an orthophoto
    in an arbitrary local frame when GPS is missing, and that output cannot be
    joined to a field or compared with satellite imagery.
    """
    project_dir = Path(project_dir)
    found: dict[str, Path] = {}
    for name, relative in EXPECTED_OUTPUTS.items():
        path = project_dir / relative
        if not path.exists() or path.stat().st_size == 0:
            continue
        if path.suffix.lower() == ".tif" and not _is_georeferenced(path):
            log.warning("%s exists but carries no CRS; treating it as unusable", path)
            continue
        found[name] = path
    return found


def _is_georeferenced(path: Path) -> bool:
    """True when a raster carries a coordinate reference system."""
    try:
        import rasterio

        with rasterio.open(path) as dataset:
            return dataset.crs is not None
    except Exception as exc:  # noqa: BLE001 - an unreadable raster is unusable
        log.warning("could not read %s: %s", path, exc)
        return False


def describe_missing(found: Iterable[str]) -> str:
    """Name the products that did not appear, for an error message."""
    missing = [name for name in EXPECTED_OUTPUTS if name not in set(found)]
    return ", ".join(missing)


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #


def run_odm(
    flight_id: str,
    raw_dir: Path,
    datasets_dir: Path,
    config: OdmConfig,
    *,
    image: str = ODM_IMAGE,
    stream: bool = True,
) -> OdmResult:
    """Stage images, run ODM in Docker, and verify what it produced.

    The full command is written to ``odm_command.txt`` inside the project and
    logged, so any run can be repeated by hand without this tool.

    Raises:
        OdmError: if Docker is unusable or the flight folder holds no images.
    """
    status = check_docker(image)
    if not status.available:
        raise OdmError("; ".join(status.problems))

    project_dir = Path(datasets_dir).resolve() / flight_id
    project_dir.mkdir(parents=True, exist_ok=True)
    n_images = stage_images(raw_dir, project_dir)

    for warning in memory_advice(n_images, status.memory_bytes):
        log.warning(warning)

    command = build_command(datasets_dir, flight_id, config, image=image)
    (project_dir / "odm_command.txt").write_text(as_shell(command) + "\n", encoding="utf-8")
    log.info("running ODM on %d image(s): %s", n_images, as_shell(command))

    log_path = project_dir / "odm_run.log"
    started = time.monotonic()
    returncode, log_text = _execute(command, log_path, stream=stream)
    duration = time.monotonic() - started

    outputs = verify_outputs(project_dir)
    failed_stage, advice = (None, "")
    if returncode != 0 or len(outputs) < len(EXPECTED_OUTPUTS):
        failed_stage, advice = diagnose(log_text, returncode)
        if returncode == 0:
            advice = (
                f"ODM exited cleanly but did not produce: "
                f"{describe_missing(outputs)}. " + advice
            )

    return OdmResult(
        flight_id=flight_id, project_dir=project_dir, command=command,
        returncode=returncode, duration_s=duration, log_path=log_path,
        outputs=outputs, failed_stage=failed_stage, advice=advice,
    )


def _execute(
    command: Sequence[str], log_path: Path, *, stream: bool
) -> tuple[int, str]:
    """Run a command, tee-ing its output to a log file and returning both."""
    lines: list[str] = []
    with subprocess.Popen(
        list(command), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    ) as process, log_path.open("w", encoding="utf-8") as handle:
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = strip_ansi(raw_line)
            handle.write(line)
            lines.append(line)
            if stream and _worth_showing(line):
                log.info("odm | %s", line.rstrip())
        returncode = process.wait()
    return returncode, "".join(lines)


def _worth_showing(line: str) -> bool:
    """Filter ODM's output down to stage banners, warnings and errors."""
    lowered = line.lower()
    return (
        "stage" in lowered
        or lowered.startswith("[error]")
        or lowered.startswith("[warning]")
        or "traceback" in lowered
    )


def load_result(project_dir: Path) -> dict[str, Path]:
    """Products of an earlier run, so later steps need not re-run ODM."""
    outputs = verify_outputs(project_dir)
    if not outputs:
        raise OdmError(
            f"no usable ODM products in {project_dir}. Run "
            "'dosojos-drone odm <flight_id>' first."
        )
    return outputs
