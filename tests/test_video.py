"""Tests for SRT telemetry parsing, sharpness filtering and frame geotagging."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image

from dosojos_drone.ingest import read_shot
from dosojos_drone.video import (
    HARD_BLUR_FLOOR,
    SrtEntry,
    VideoError,
    blur_threshold,
    build_exif_bytes,
    entry_for_time,
    find_ffmpeg,
    parse_srt,
    variance_of_laplacian,
    write_gps_exif,
)

# The layout current DJI firmware writes: several pairs share one bracket.
MODERN_SRT = """1
00:00:00,000 --> 00:00:00,033
<font size="28">FrameCnt: 1, DiffTime: 33ms
2026-09-06 16:05:00.000
[iso : 100] [shutter : 1/1000] [fnum : 280] [latitude : 26.2161880] \
[longitude : -97.9134091] [rel_alt: 60.000 abs_alt: 72.000] </font>

2
00:00:00,033 --> 00:00:00,066
<font size="28">FrameCnt: 2, DiffTime: 33ms
2026-09-06 16:05:01.000
[iso : 100] [latitude : 26.2162880] [longitude : -97.9134091] \
[rel_alt: 60.500 abs_alt: 72.500] </font>
"""

# Older firmware wrote a GPS tuple instead of bracketed pairs.
LEGACY_SRT = """1
00:00:00,000 --> 00:00:01,000
HOME(-97.9134,26.2161) 2026.09.06 16:05:00
GPS(-97.9134091,26.2161880,12) BAROMETER:60.0
"""


def _write(tmp_path: Path, text: str, name: str = "demo.SRT") -> Path:
    """Write an SRT fixture."""
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# SRT parsing
# --------------------------------------------------------------------------- #


def test_modern_srt_is_parsed(tmp_path: Path) -> None:
    """Position, both altitudes and the wall clock all come out of one record."""
    entries = parse_srt(_write(tmp_path, MODERN_SRT))
    assert len(entries) == 2

    first = entries[0]
    assert first.lat == pytest.approx(26.2161880)
    assert first.lon == pytest.approx(-97.9134091)
    assert first.rel_alt_m == pytest.approx(60.0)
    assert first.abs_alt_m == pytest.approx(72.0)
    assert first.timestamp == datetime(2026, 9, 6, 16, 5, 0)
    assert (first.start_s, first.end_s) == (0.0, pytest.approx(0.033))


def test_two_pairs_in_one_bracket_are_both_read(tmp_path: Path) -> None:
    """DJI packs rel_alt and abs_alt into a single bracket.

    Treating a bracket as exactly one key/value pair reads the whole of
    "60.000 abs_alt: 72.000" as one value, which parses as nothing and silently
    leaves every frame without an altitude.
    """
    entry = parse_srt(_write(tmp_path, MODERN_SRT))[0]
    assert entry.rel_alt_m == 60.0
    assert entry.abs_alt_m == 72.0


def test_legacy_gps_tuple_is_parsed(tmp_path: Path) -> None:
    """Older firmware wrote GPS(lon,lat,sats); the format has no version marker."""
    entry = parse_srt(_write(tmp_path, LEGACY_SRT))[0]
    assert entry.lon == pytest.approx(-97.9134091)
    assert entry.lat == pytest.approx(26.2161880)


def test_wall_clock_is_not_mistaken_for_telemetry(tmp_path: Path) -> None:
    """"16:05:00" outside the brackets must not parse as a key of 16."""
    entry = parse_srt(_write(tmp_path, MODERN_SRT))[0]
    assert entry.lat == pytest.approx(26.2161880)     # not 16 or 5


def test_srt_without_positions_fails_clearly(tmp_path: Path) -> None:
    """A subtitle track is not telemetry, and the message should say so."""
    path = _write(tmp_path, "1\n00:00:00,000 --> 00:00:01,000\nhello world\n")
    with pytest.raises(VideoError, match="no readable GPS"):
        parse_srt(path)


def test_missing_srt_names_the_file(tmp_path: Path) -> None:
    """A wrong path fails with the path, not a bare OSError."""
    with pytest.raises(VideoError, match="could not read"):
        parse_srt(tmp_path / "nope.SRT")


# --------------------------------------------------------------------------- #
# Frame to telemetry alignment
# --------------------------------------------------------------------------- #


def _entries() -> list[SrtEntry]:
    """Three one-second telemetry records."""
    return [
        SrtEntry(index=i, start_s=float(i), end_s=float(i + 1),
                 lon=-97.9 + i * 0.001, lat=26.2)
        for i in range(3)
    ]


def test_frame_matches_the_record_covering_its_moment() -> None:
    """A frame at 1.5 s belongs to the record spanning 1 to 2 s."""
    assert entry_for_time(_entries(), 1.5).index == 1
    assert entry_for_time(_entries(), 0.0).index == 0


def test_time_past_the_end_falls_back_to_the_nearest_record() -> None:
    """ffmpeg lands frames on record boundaries often enough to matter."""
    assert entry_for_time(_entries(), 99.0).index == 2
    assert entry_for_time(_entries(), -5.0).index == 0


def test_no_telemetry_yields_no_match() -> None:
    """An empty SRT gives nothing to match against."""
    assert entry_for_time([], 1.0) is None


# --------------------------------------------------------------------------- #
# Sharpness
# --------------------------------------------------------------------------- #


def test_blurred_image_scores_below_a_sharp_one(tmp_path: Path) -> None:
    """Variance of the Laplacian falls as high-frequency detail is smeared away."""
    from PIL import ImageFilter
    import numpy as np

    rng = np.random.default_rng(0)
    noise = (rng.random((120, 160, 3)) * 255).astype("uint8")
    sharp_path = tmp_path / "sharp.jpg"
    blurred_path = tmp_path / "blurred.jpg"
    Image.fromarray(noise).save(sharp_path, quality=95)
    Image.fromarray(noise).filter(ImageFilter.GaussianBlur(5)).save(
        blurred_path, quality=95
    )

    assert variance_of_laplacian(sharp_path) > variance_of_laplacian(blurred_path)


def test_unreadable_frame_scores_zero(tmp_path: Path) -> None:
    """A corrupt frame must sort to the bottom, not raise."""
    path = tmp_path / "broken.jpg"
    path.write_bytes(b"not a jpeg")
    assert variance_of_laplacian(path) == 0.0


def test_absolute_threshold_overrides_the_quantile() -> None:
    """An explicit cut-off is used exactly as given."""
    assert blur_threshold([10, 20, 30], absolute=25.0) == 25.0


def test_quantile_threshold_adapts_to_the_flight() -> None:
    """Absolute sharpness depends on scene content, so the cut is relative."""
    scores = [float(v) for v in range(100, 1100, 10)]
    cut = blur_threshold(scores, quantile=0.20)
    assert 250 <= cut <= 350
    assert sum(1 for s in scores if s < cut) == pytest.approx(20, abs=2)


def test_threshold_never_falls_below_the_hard_floor() -> None:
    """A uniformly soft video should still lose its worst frames."""
    assert blur_threshold([1.0, 2.0, 3.0], quantile=0.5) == HARD_BLUR_FLOOR
    assert blur_threshold([], quantile=0.5) == HARD_BLUR_FLOOR


# --------------------------------------------------------------------------- #
# Geotagging
# --------------------------------------------------------------------------- #


def test_written_exif_reads_back_through_the_ingest_path(tmp_path: Path) -> None:
    """A geotagged frame must satisfy the same reader step 1 uses.

    This is the join between the two halves of the video path: what the SRT
    parser produced has to survive into EXIF and back out again.
    """
    path = tmp_path / "frame_00001.jpg"
    Image.new("RGB", (640, 480), (60, 120, 60)).save(path)
    entry = SrtEntry(
        index=1, start_s=0.0, end_s=0.5, lon=-97.9134091, lat=26.2161880,
        abs_alt_m=72.0, rel_alt_m=60.0, timestamp=datetime(2026, 9, 6, 16, 5, 0),
    )
    write_gps_exif(path, entry)

    shot = read_shot(path)
    assert shot.has_gps
    assert shot.lon == pytest.approx(-97.9134091, abs=1e-5)
    assert shot.lat == pytest.approx(26.2161880, abs=1e-5)
    assert shot.alt_m == pytest.approx(72.0, abs=0.1)
    assert shot.timestamp == datetime(2026, 9, 6, 16, 5, 0)
    assert shot.camera == "FC6310"


def test_southern_and_eastern_positions_keep_their_sign(tmp_path: Path) -> None:
    """EXIF stores magnitude plus a hemisphere ref, so signs must round-trip."""
    path = tmp_path / "f.jpg"
    Image.new("RGB", (64, 48)).save(path)
    write_gps_exif(
        path,
        SrtEntry(index=1, start_s=0, end_s=1, lon=151.2, lat=-33.87, abs_alt_m=50.0),
    )
    shot = read_shot(path)
    assert shot.lon == pytest.approx(151.2, abs=1e-4)
    assert shot.lat == pytest.approx(-33.87, abs=1e-4)


def test_exif_carries_the_lens_fields_odm_needs() -> None:
    """Focal length and focal plane resolution let the reader recover sensor width."""
    blob = build_exif_bytes(
        lon=-97.9, lat=26.2, altitude_m=72.0, when=datetime(2026, 9, 6, 16, 5, 0),
        width=5472, height=3648,
    )
    import piexif

    parsed = piexif.load(blob)
    assert parsed["0th"][piexif.ImageIFD.Model] == b"FC6310"
    assert piexif.ExifIFD.FocalLength in parsed["Exif"]
    assert piexif.ExifIFD.FocalPlaneXResolution in parsed["Exif"]


def test_altitude_is_omitted_when_telemetry_lacks_it(tmp_path: Path) -> None:
    """A record with no altitude should not fabricate one."""
    path = tmp_path / "f.jpg"
    Image.new("RGB", (64, 48)).save(path)
    write_gps_exif(path, SrtEntry(index=1, start_s=0, end_s=1, lon=-97.9, lat=26.2))
    shot = read_shot(path)
    assert shot.has_gps
    assert shot.alt_m is None


# --------------------------------------------------------------------------- #
# ffmpeg
# --------------------------------------------------------------------------- #


def test_ffmpeg_is_available() -> None:
    """Either a system ffmpeg or the bundled build must be resolvable."""
    assert Path(find_ffmpeg()).exists()
