"""Video fallback: turn an MP4 plus a DJI SRT into geotagged JPEGs for ODM.

Strictly the secondary path. Video frames are compressed, rolling-shutter
distorted and motion blurred in ways stills are not, so a reconstruction built
from them is measurably worse. Use it when video is all that exists.

The chain is: extract frames at a low frame rate, score each for sharpness,
drop the blurriest, then write EXIF GPS onto the survivors from the SRT so ODM
can georeference them.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Sequence

import numpy as np
import piexif

log = logging.getLogger(__name__)

#: Frames per second to pull out of the video. Two is usually plenty: at typical
#: survey speed it already gives more forward overlap than a stills flight.
DEFAULT_FPS = 2.0

#: Fraction of frames discarded as the blurriest, when no absolute threshold is set.
DEFAULT_BLUR_QUANTILE = 0.15

#: Absolute variance-of-Laplacian below which a frame is unusable regardless of
#: how the rest of the flight scored. A frame this soft has no usable features.
HARD_BLUR_FLOOR = 20.0


class VideoError(RuntimeError):
    """Raised when the video path cannot proceed, with what to do about it."""


@dataclass(frozen=True)
class SrtEntry:
    """One telemetry record from a DJI SRT sidecar."""

    index: int
    start_s: float
    end_s: float
    lon: float
    lat: float
    abs_alt_m: float | None = None
    rel_alt_m: float | None = None
    timestamp: datetime | None = None

    def contains(self, seconds: float) -> bool:
        """True when a video timestamp falls inside this record."""
        return self.start_s <= seconds < self.end_s


@dataclass(frozen=True)
class FrameResult:
    """One extracted frame and what became of it."""

    path: Path
    video_time_s: float
    sharpness: float
    kept: bool
    reason: str = ""


@dataclass(frozen=True)
class VideoIngest:
    """Outcome of turning one video into a frame folder."""

    flight_id: str
    video: Path
    srt: Path | None
    out_dir: Path
    frames: list[FrameResult]
    fps: float
    blur_threshold: float

    @property
    def kept(self) -> list[FrameResult]:
        """Frames that survived blur filtering and carry GPS."""
        return [f for f in self.frames if f.kept]


# --------------------------------------------------------------------------- #
# ffmpeg
# --------------------------------------------------------------------------- #


def find_ffmpeg() -> str:
    """Locate an ffmpeg binary, preferring one on PATH then the bundled build.

    Raises:
        VideoError: naming both options if neither is available.
    """
    on_path = shutil.which("ffmpeg")
    if on_path:
        return on_path
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # noqa: BLE001 - any import or download failure
        raise VideoError(
            "ffmpeg was not found. Either install it and put it on PATH, or "
            f"install the bundled build with: pip install imageio-ffmpeg  ({exc})"
        ) from exc


def probe_duration_s(video: Path, ffmpeg: str | None = None) -> float | None:
    """Video duration in seconds, parsed from ffmpeg's own report."""
    ffmpeg = ffmpeg or find_ffmpeg()
    result = subprocess.run(
        [ffmpeg, "-i", str(video)], capture_output=True, text=True, check=False
    )
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", result.stderr)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def extract_frames(
    video: Path, out_dir: Path, *, fps: float = DEFAULT_FPS, ffmpeg: str | None = None
) -> list[Path]:
    """Pull frames out of a video at a fixed rate, returning them in order.

    Raises:
        VideoError: if ffmpeg fails, quoting its own error output.
    """
    ffmpeg = ffmpeg or find_ffmpeg()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("frame_*.jpg"):
        stale.unlink()

    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(video),
        "-vf", f"fps={fps}", "-q:v", "2",
        str(out_dir / "frame_%05d.jpg"),
    ]
    log.info("running: %s", " ".join(command))
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise VideoError(
            f"ffmpeg failed extracting frames from {video.name} "
            f"(exit {result.returncode}): {result.stderr.strip()[:400]}"
        )

    frames = sorted(out_dir.glob("frame_*.jpg"))
    if not frames:
        raise VideoError(
            f"ffmpeg produced no frames from {video.name}. Check the file is a "
            "readable video and that the requested fps is below its frame rate."
        )
    log.info("extracted %d frame(s) at %.1f fps", len(frames), fps)
    return frames


# --------------------------------------------------------------------------- #
# SRT telemetry
# --------------------------------------------------------------------------- #

_TIMECODE = re.compile(
    r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)"
)
#: DJI writes telemetry inside square brackets, but a single bracket can hold
#: more than one pair, as in "[rel_alt: 60.000 abs_alt: 72.000]". So the bracket
#: is matched first and its contents scanned for pairs, rather than assuming one
#: pair per bracket.
_BRACKET = re.compile(r"\[([^\]]*)\]")
_KEYVAL = re.compile(r"([A-Za-z_]+)\s*:\s*([^\s\]\[]+)")
#: Older firmware writes a GPS(lon,lat,sats) tuple instead.
_GPS_TUPLE = re.compile(r"GPS\s*\(\s*([-\d.]+)\s*,\s*([-\d.]+)")
_DATETIME = re.compile(r"(\d{4})[-.](\d{2})[-.](\d{2})[ T](\d{2}):(\d{2}):(\d{2})")


def _telemetry_fields(block: str) -> dict[str, str]:
    """Every key/value pair inside the brackets of one SRT record.

    Only bracket contents are scanned, which keeps the wall-clock line from being
    misread as telemetry: its "16:05:00" would otherwise parse as a key of 16.
    """
    fields: dict[str, str] = {}
    for content in _BRACKET.findall(block):
        for key, value in _KEYVAL.findall(content):
            fields[key.lower()] = value
    return fields


def _timecode_seconds(match: re.Match[str]) -> tuple[float, float]:
    """Convert an SRT timecode line into start and end seconds."""
    values = [int(group) for group in match.groups()]
    start = values[0] * 3600 + values[1] * 60 + values[2] + values[3] / 1000.0
    end = values[4] * 3600 + values[5] * 60 + values[6] + values[7] / 1000.0
    return start, end


def _float_or_none(text: str | None) -> float | None:
    """Parse a telemetry value, tolerating units and stray characters."""
    if text is None:
        return None
    cleaned = re.sub(r"[^\d.+-]", "", text)
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_srt(path: Path) -> list[SrtEntry]:
    """Parse a DJI SRT sidecar into per-record telemetry.

    Handles both the bracketed ``[latitude : x]`` layouts and the older
    ``GPS(lon,lat,sats)`` form, since DJI has changed the format across firmware
    and the file gives no version marker.

    Raises:
        VideoError: if no record in the file carries a position.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        raise VideoError(f"could not read {path}: {exc}") from exc

    entries: list[SrtEntry] = []
    for index, block in enumerate(re.split(r"\n\s*\n", text)):
        if not block.strip():
            continue
        timecode = _TIMECODE.search(block)
        if not timecode:
            continue
        start_s, end_s = _timecode_seconds(timecode)

        fields = _telemetry_fields(block)
        lat = _float_or_none(fields.get("latitude"))
        lon = _float_or_none(fields.get("longitude"))
        if lon is None or lat is None:
            tuple_match = _GPS_TUPLE.search(block)
            if tuple_match:
                lon = float(tuple_match.group(1))
                lat = float(tuple_match.group(2))
        if lon is None or lat is None:
            continue

        entries.append(
            SrtEntry(
                index=index,
                start_s=start_s,
                end_s=end_s,
                lon=lon,
                lat=lat,
                abs_alt_m=_float_or_none(fields.get("abs_alt")),
                rel_alt_m=_float_or_none(fields.get("rel_alt")),
                timestamp=_srt_timestamp(block),
            )
        )

    if not entries:
        raise VideoError(
            f"{path.name} contains no readable GPS records. Confirm it is the SRT "
            "the drone wrote alongside the video, not a subtitle track."
        )
    log.info("parsed %d telemetry record(s) from %s", len(entries), path.name)
    return entries


def _srt_timestamp(block: str) -> datetime | None:
    """Wall-clock capture time from an SRT block, if it carries one."""
    match = _DATETIME.search(block)
    if not match:
        return None
    year, month, day, hour, minute, second = (int(g) for g in match.groups())
    try:
        return datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None


def entry_for_time(entries: Sequence[SrtEntry], seconds: float) -> SrtEntry | None:
    """Telemetry record covering a video timestamp, or the nearest one.

    Falling back to the nearest record matters because ffmpeg's frame rate filter
    lands a frame on the boundary between records often enough to matter.
    """
    if not entries:
        return None
    for entry in entries:
        if entry.contains(seconds):
            return entry
    return min(entries, key=lambda e: abs(0.5 * (e.start_s + e.end_s) - seconds))


# --------------------------------------------------------------------------- #
# Sharpness
# --------------------------------------------------------------------------- #


def variance_of_laplacian(path: Path) -> float:
    """Sharpness score for one frame: the variance of its Laplacian.

    A blurred frame has little high-frequency content, so its second derivative
    varies little. The absolute value depends on scene content, which is why the
    default threshold is a quantile of this flight rather than a fixed number.
    """
    import cv2

    image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        return 0.0
    return float(cv2.Laplacian(image, cv2.CV_64F).var())


def blur_threshold(
    scores: Sequence[float],
    *,
    absolute: float | None = None,
    quantile: float = DEFAULT_BLUR_QUANTILE,
) -> float:
    """Decide the sharpness cut-off for one flight.

    An absolute threshold wins when given. Otherwise the cut is a quantile of
    this flight's own scores, floored so a uniformly soft video still loses its
    worst frames rather than keeping everything.
    """
    if absolute is not None:
        return absolute
    if not scores:
        return HARD_BLUR_FLOOR
    return max(float(np.quantile(np.asarray(scores, dtype=float), quantile)),
               HARD_BLUR_FLOOR)


# --------------------------------------------------------------------------- #
# EXIF
# --------------------------------------------------------------------------- #


def _deg_to_dms_rational(value: float) -> tuple[tuple[int, int], ...]:
    """Signed decimal degrees to the EXIF degrees/minutes/seconds triple."""
    value = abs(value)
    degrees = int(value)
    minutes_float = (value - degrees) * 60
    minutes = int(minutes_float)
    seconds = round((minutes_float - minutes) * 60, 5)
    return ((degrees, 1), (minutes, 1), (int(seconds * 100000), 100000))


def _rational(value: float, denominator: int = 1000) -> tuple[int, int]:
    """Express a float as an EXIF rational."""
    fraction = Fraction(value).limit_denominator(denominator)
    return fraction.numerator, fraction.denominator


def build_exif_bytes(
    *,
    lon: float,
    lat: float,
    altitude_m: float | None,
    when: datetime | None,
    width: int,
    height: int,
    camera: str = "FC6310",
    make: str = "DJI",
    focal_mm: float = 8.8,
    sensor_width_mm: float = 13.2,
    software: str = "dosojos video ingest",
) -> bytes:
    """Assemble an EXIF block with the GPS and lens fields ODM needs.

    Focal plane resolution is written in pixels per millimetre so the sensor
    width is recoverable without the camera appearing in any lookup table.
    """
    stamp = (when or datetime(1970, 1, 1)).strftime("%Y:%m:%d %H:%M:%S")
    zeroth = {
        piexif.ImageIFD.Make: make,
        piexif.ImageIFD.Model: camera,
        piexif.ImageIFD.DateTime: stamp,
        piexif.ImageIFD.Software: software,
    }
    exif = {
        piexif.ExifIFD.DateTimeOriginal: stamp,
        piexif.ExifIFD.FocalLength: _rational(focal_mm),
        piexif.ExifIFD.PixelXDimension: width,
        piexif.ExifIFD.PixelYDimension: height,
        piexif.ExifIFD.FocalPlaneXResolution: _rational(width / sensor_width_mm, 100000),
        piexif.ExifIFD.FocalPlaneResolutionUnit: 4,
    }
    gps = {
        piexif.GPSIFD.GPSLatitudeRef: "N" if lat >= 0 else "S",
        piexif.GPSIFD.GPSLatitude: _deg_to_dms_rational(lat),
        piexif.GPSIFD.GPSLongitudeRef: "E" if lon >= 0 else "W",
        piexif.GPSIFD.GPSLongitude: _deg_to_dms_rational(lon),
    }
    if altitude_m is not None:
        gps[piexif.GPSIFD.GPSAltitudeRef] = 0 if altitude_m >= 0 else 1
        gps[piexif.GPSIFD.GPSAltitude] = _rational(abs(altitude_m), 100)
    return piexif.dump(
        {"0th": zeroth, "Exif": exif, "GPS": gps, "1st": {}, "thumbnail": None}
    )


def write_gps_exif(path: Path, entry: SrtEntry, **camera_kwargs) -> None:
    """Stamp one extracted frame with the telemetry for its moment."""
    from PIL import Image

    with Image.open(path) as image:
        width, height = image.size
        pixels = image.copy()

    altitude = entry.abs_alt_m if entry.abs_alt_m is not None else entry.rel_alt_m
    pixels.save(
        path,
        quality=95,
        exif=build_exif_bytes(
            lon=entry.lon, lat=entry.lat, altitude_m=altitude,
            when=entry.timestamp, width=width, height=height, **camera_kwargs,
        ),
    )


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #


def video_to_frames(
    flight_id: str,
    video: Path,
    srt: Path | None,
    out_dir: Path,
    *,
    fps: float = DEFAULT_FPS,
    blur_quantile: float = DEFAULT_BLUR_QUANTILE,
    absolute_blur_threshold: float | None = None,
    camera_kwargs: dict | None = None,
) -> VideoIngest:
    """Extract, filter and geotag frames from a video into a flight folder.

    Frames that fail the sharpness cut, or that have no telemetry covering their
    moment, are deleted rather than left behind, so the folder that remains is
    exactly what ODM should be given.
    """
    video, out_dir = Path(video), Path(out_dir)
    if not video.exists():
        raise VideoError(f"video not found: {video}")

    entries = parse_srt(srt) if srt else []
    if not entries:
        log.warning(
            "no SRT supplied: frames will be extracted but carry no GPS, so the "
            "reconstruction will not be georeferenced"
        )

    frames = extract_frames(video, out_dir, fps=fps)
    scores = [variance_of_laplacian(frame) for frame in frames]
    threshold = blur_threshold(
        scores, absolute=absolute_blur_threshold, quantile=blur_quantile
    )

    results: list[FrameResult] = []
    camera_kwargs = camera_kwargs or {}
    for position, (frame, score) in enumerate(zip(frames, scores)):
        video_time = position / fps
        entry = entry_for_time(entries, video_time) if entries else None

        if score < threshold:
            results.append(FrameResult(frame, video_time, score, False,
                                       f"blurry ({score:.0f} < {threshold:.0f})"))
            frame.unlink(missing_ok=True)
            continue
        if entries and entry is None:
            results.append(FrameResult(frame, video_time, score, False,
                                       "no telemetry for this moment"))
            frame.unlink(missing_ok=True)
            continue

        if entry is not None:
            write_gps_exif(frame, entry, **camera_kwargs)
        results.append(FrameResult(frame, video_time, score, True))

    kept = sum(1 for r in results if r.kept)
    log.info(
        "kept %d of %d frame(s); sharpness cut-off %.0f", kept, len(results), threshold
    )
    if kept == 0:
        raise VideoError(
            "every frame was rejected. Lower --blur-quantile, or check the video "
            "is not uniformly out of focus."
        )
    return VideoIngest(
        flight_id=flight_id, video=video, srt=Path(srt) if srt else None,
        out_dir=out_dir, frames=results, fps=fps, blur_threshold=threshold,
    )
