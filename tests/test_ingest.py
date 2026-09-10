"""Tests for EXIF ingest, survey geometry and the pre-flight checks."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from make_synthetic_flight import build_exif  # noqa: E402

from dosojos_drone.ingest import (
    MIN_FORWARD_OVERLAP,
    FlightSurvey,
    IngestError,
    Shot,
    check_survey,
    estimate_gsd_cm,
    footprint_m,
    forward_overlap,
    haversine_m,
    read_shot,
    read_shots,
    survey_flight,
)

# A Phantom 4 Pro at 60 m: sensor 13.2 mm, focal 8.8 mm.
SENSOR_MM = 13.2
FOCAL_MM = 8.8


def _write_jpeg(
    folder: Path,
    name: str,
    *,
    lon: float = -97.912,
    lat: float = 26.218,
    alt: float = 72.0,
    when: datetime | None = None,
    width: int = 400,
    height: int = 300,
    gps: bool = True,
) -> Path:
    """Write one small JPEG, with or without EXIF GPS."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    image = Image.new("RGB", (width, height), (60, 120, 60))
    when = when or datetime(2026, 9, 5, 15, 30, 0)
    if gps:
        image.save(path, exif=build_exif(lon, lat, alt, when, width, height))
    else:
        image.save(path)
    return path


# --------------------------------------------------------------------------- #
# EXIF
# --------------------------------------------------------------------------- #


def test_gps_is_read_from_the_gps_sub_ifd(tmp_path: Path) -> None:
    """The GPS tag is an offset pointer, not a dict.

    Reading the base IFD alone silently reports every frame as having no
    position, which is the failure this guards against.
    """
    path = _write_jpeg(tmp_path, "a.JPG", lon=-97.912, lat=26.218)
    shot = read_shot(path)
    assert shot.has_gps
    assert shot.lon == pytest.approx(-97.912, abs=1e-5)
    assert shot.lat == pytest.approx(26.218, abs=1e-5)


def test_western_longitude_stays_negative(tmp_path: Path) -> None:
    """EXIF stores magnitude plus a hemisphere ref; the RGV is west and north."""
    shot = read_shot(_write_jpeg(tmp_path, "w.JPG", lon=-97.9, lat=26.2))
    assert shot.lon < 0
    assert shot.lat > 0


def test_camera_and_lens_are_read(tmp_path: Path) -> None:
    """Model, focal length and derived sensor width all come out of EXIF."""
    shot = read_shot(_write_jpeg(tmp_path, "a.JPG", width=400))
    assert shot.camera == "FC6310"
    assert shot.focal_mm == pytest.approx(FOCAL_MM, abs=0.01)
    assert shot.sensor_width_mm == pytest.approx(SENSOR_MM, rel=1e-3)
    assert shot.timestamp == datetime(2026, 9, 5, 15, 30, 0)


def test_frame_without_gps_is_kept_but_marked(tmp_path: Path) -> None:
    """A frame missing GPS must still be counted, not dropped silently."""
    shot = read_shot(_write_jpeg(tmp_path, "n.JPG", gps=False))
    assert shot.has_gps is False
    assert shot.width_px == 400


def test_empty_and_missing_folders_fail_clearly(tmp_path: Path) -> None:
    """Both cases name the folder rather than raising something opaque."""
    with pytest.raises(IngestError, match="not a directory"):
        read_shots(tmp_path / "nope")
    (tmp_path / "empty").mkdir()
    with pytest.raises(IngestError, match="no JPEG"):
        read_shots(tmp_path / "empty")


def test_shots_are_ordered_by_capture_time(tmp_path: Path) -> None:
    """Spacing between consecutive frames depends on flight order, not filename."""
    _write_jpeg(tmp_path, "z.JPG", when=datetime(2026, 9, 5, 15, 0, 0))
    _write_jpeg(tmp_path, "a.JPG", when=datetime(2026, 9, 5, 15, 5, 0))
    assert [s.path.name for s in read_shots(tmp_path)] == ["z.JPG", "a.JPG"]


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #


def test_haversine_against_a_known_separation() -> None:
    """One degree of latitude is about 111 km anywhere."""
    assert haversine_m(-97.9, 26.0, -97.9, 27.0) == pytest.approx(111_195, rel=0.01)
    assert haversine_m(-97.9, 26.2, -97.9, 26.2) == 0.0


def test_gsd_matches_the_textbook_formula() -> None:
    """GSD = altitude * sensor_width / (focal_length * image_width)."""
    shot = Shot(
        path=Path("x.jpg"), lon=0, lat=0, alt_m=72, timestamp=None,
        focal_mm=FOCAL_MM, camera="FC6310", width_px=5472, height_px=3648,
        sensor_width_mm=SENSOR_MM,
    )
    expected_cm = (60.0 * SENSOR_MM) / (FOCAL_MM * 5472) * 100
    assert estimate_gsd_cm(shot, 60.0) == pytest.approx(expected_cm, rel=1e-9)
    assert estimate_gsd_cm(shot, 60.0) == pytest.approx(1.645, abs=0.005)


def test_gsd_is_none_when_sensor_width_is_unknown() -> None:
    """A guessed sensor size would produce a confidently wrong GSD."""
    shot = Shot(
        path=Path("x.jpg"), lon=0, lat=0, alt_m=72, timestamp=None,
        focal_mm=FOCAL_MM, camera="MysteryCam", width_px=4000, height_px=3000,
        sensor_width_mm=None,
    )
    assert estimate_gsd_cm(shot, 60.0) is None


def test_footprint_scales_with_pixel_count_and_gsd() -> None:
    """Footprint is just image size in pixels times ground size per pixel."""
    shot = Shot(
        path=Path("x.jpg"), lon=0, lat=0, alt_m=72, timestamp=None,
        focal_mm=FOCAL_MM, camera="c", width_px=5472, height_px=3648,
        sensor_width_mm=SENSOR_MM,
    )
    across, along = footprint_m(shot, 1.645)
    assert across == pytest.approx(90.0, abs=0.5)
    assert along == pytest.approx(60.0, abs=0.5)


@pytest.mark.parametrize(
    ("spacing", "along", "expected"),
    [
        (12.0, 60.0, 0.80),      # standard mapping overlap
        (30.0, 60.0, 0.50),
        (60.0, 60.0, 0.00),      # frames just touch
        (90.0, 60.0, 0.00),      # a gap clamps to zero, never negative
        (0.0, 60.0, 1.00),       # hovering clamps to one, never above
    ],
)
def test_forward_overlap_is_clamped(spacing: float, along: float, expected: float) -> None:
    """Overlap is a fraction, so it cannot fall below zero or exceed one."""
    assert forward_overlap(spacing, along) == pytest.approx(expected)


# --------------------------------------------------------------------------- #
# Survey and checks
# --------------------------------------------------------------------------- #


def _survey(**kwargs) -> FlightSurvey:
    """A survey with plausible defaults, overridden per test."""
    defaults = dict(
        flight_id="f", folder=Path("."),
        shots=[
            Shot(path=Path(f"{i}.jpg"), lon=-97.9, lat=26.2, alt_m=72.0,
                 timestamp=None, focal_mm=FOCAL_MM, camera="FC6310",
                 width_px=5472, height_px=3648, sensor_width_mm=SENSOR_MM)
            for i in range(30)
        ],
        bounds_wgs84=(-97.91, 26.21, -97.90, 26.22), gsd_cm=1.6,
        footprint_m=(90.0, 60.0), mean_spacing_m=12.0, forward_overlap=0.80,
        alt_mean_m=72.0, alt_variation=0.02, duration_s=400.0, warnings=[],
    )
    defaults.update(kwargs)
    return FlightSurvey(**defaults)


def test_a_good_survey_raises_nothing() -> None:
    """A textbook mapping flight produces no complaints at all."""
    assert check_survey(_survey()) == []
    assert _survey().usable is True


def test_thin_overlap_blocks_the_run() -> None:
    """Below 70% forward overlap, dense reconstruction will leave holes."""
    problems = check_survey(_survey(forward_overlap=0.55))
    assert any(p.startswith("BLOCKER") and "overlap" in p for p in problems)
    assert f"{MIN_FORWARD_OVERLAP:.0%}" in " ".join(problems)


def test_wandering_altitude_warns_without_blocking() -> None:
    """Uneven altitude spoils GSD consistency but still reconstructs."""
    problems = check_survey(_survey(alt_variation=0.30))
    assert any("altitude varies" in p for p in problems)
    assert not any(p.startswith("BLOCKER") for p in problems)


def test_no_gps_anywhere_is_a_blocker() -> None:
    """Without GPS nothing is georeferenced, so nothing joins to a field."""
    shots = [
        Shot(path=Path("a.jpg"), lon=None, lat=None, alt_m=None, timestamp=None,
             focal_mm=FOCAL_MM, camera="FC6310", width_px=5472, height_px=3648,
             sensor_width_mm=SENSOR_MM)
        for _ in range(30)
    ]
    problems = check_survey(_survey(shots=shots))
    assert any(p.startswith("BLOCKER") and "GPS" in p for p in problems)


def test_partial_gps_loss_warns_but_proceeds() -> None:
    """A few unpositioned frames can still be placed by feature matching."""
    shots = _survey().shots[:]
    shots[0] = Shot(
        path=Path("a.jpg"), lon=None, lat=None, alt_m=None, timestamp=None,
        focal_mm=FOCAL_MM, camera="FC6310", width_px=5472, height_px=3648,
        sensor_width_mm=SENSOR_MM,
    )
    problems = check_survey(_survey(shots=shots))
    assert any("lack GPS" in p for p in problems)
    assert not any(p.startswith("BLOCKER") for p in problems)


def test_too_few_images_is_a_blocker() -> None:
    """Photogrammetry needs a real set, not a handful of frames."""
    problems = check_survey(_survey(shots=_survey().shots[:4]))
    assert any(p.startswith("BLOCKER") for p in problems)


def test_missing_overlap_names_the_actual_cause() -> None:
    """The message must name what is missing, not blame the last check to run."""
    shots = [
        Shot(path=Path("a.jpg"), lon=-97.9, lat=26.2, alt_m=None, timestamp=None,
             focal_mm=FOCAL_MM, camera="FC6310", width_px=5472, height_px=3648,
             sensor_width_mm=SENSOR_MM)
        for _ in range(30)
    ]
    problems = check_survey(_survey(shots=shots, forward_overlap=None, alt_mean_m=None))
    assert any("GPS altitude" in p for p in problems)


def test_survey_end_to_end_on_a_small_flight(tmp_path: Path) -> None:
    """A folder of geotagged frames produces a complete, sane survey."""
    folder = tmp_path / "flight"
    for index in range(20):
        _write_jpeg(
            folder, f"DJI_{index:03d}.JPG",
            lat=26.2160 + index * 0.000108,       # about 12 m apart
            when=datetime(2026, 9, 5, 15, 30, index * 2),
        )
    survey = survey_flight("t", folder, ground_elevation_m=12.0)

    assert survey.n_images == 20
    assert survey.n_with_gps == 20
    assert survey.alt_mean_m == pytest.approx(72.0, abs=0.5)
    assert survey.mean_spacing_m == pytest.approx(12.0, abs=1.0)
    assert survey.forward_overlap is not None and survey.forward_overlap > 0.7
    assert survey.bounds_wgs84 is not None


# --------------------------------------------------------------------------- #
# Flying height
# --------------------------------------------------------------------------- #


def test_flying_height_is_unknown_without_a_reference(tmp_path: Path) -> None:
    """Absolute GPS altitude alone cannot give height above ground.

    The lowest altitude in a survey is still flying altitude, because a mapping
    drone never descends to the ground. Treating it as the ground datum returned
    roughly the altitude jitter and produced a false overlap failure on a
    perfectly good flight.
    """
    folder = tmp_path / "f"
    for index in range(20):
        _write_jpeg(
            folder, f"a{index:03d}.JPG",
            lat=26.2160 + index * 0.000108, alt=72.0 + (index % 5),
            when=datetime(2026, 9, 5, 15, 30, index * 2),
        )
    survey = survey_flight("t", folder)          # no ground elevation given
    assert survey.gsd_cm is None
    assert survey.forward_overlap is None
    assert any("--ground-elevation" in w for w in survey.warnings)
    assert not any(w.startswith("BLOCKER") for w in survey.warnings)


def test_supplied_ground_elevation_restores_the_checks(tmp_path: Path) -> None:
    """With a ground datum the same frames yield a real GSD and overlap."""
    folder = tmp_path / "f"
    for index in range(20):
        _write_jpeg(
            folder, f"a{index:03d}.JPG",
            lat=26.2160 + index * 0.000108,
            when=datetime(2026, 9, 5, 15, 30, index * 2),
        )
    survey = survey_flight("t", folder, ground_elevation_m=12.0)
    assert survey.gsd_cm is not None
    assert survey.forward_overlap == pytest.approx(0.80, abs=0.05)


def test_xmp_relative_altitude_is_preferred(tmp_path: Path) -> None:
    """DJI's XMP relative altitude is the reliable source and wins outright."""
    from dosojos_drone.ingest import read_relative_altitude

    path = _write_jpeg(tmp_path, "x.JPG")
    assert read_relative_altitude(path) is None      # synthetic frames carry no XMP

    raw = bytearray(path.read_bytes())
    marker = b'<x:xmpmeta><drone-dji:RelativeAltitude="+61.30"/></x:xmpmeta>'
    raw[2:2] = marker                                 # splice XMP after the SOI
    path.write_bytes(bytes(raw))
    assert read_relative_altitude(path) == pytest.approx(61.30)
