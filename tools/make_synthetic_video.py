"""Generate a synthetic drone video plus a DJI-style SRT sidecar, for testing.

Produces an MP4 flown along a line over a field polygon, with a matching SRT in
the bracketed telemetry format DJI writes. Some frames are deliberately blurred
so the sharpness filter has something to reject.

Usage:
    python tools/make_synthetic_video.py --out data/video/demo.mp4 --seconds 20
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter


def field_bounds(fields_geojson: Path, field_id: str) -> tuple[float, float, float, float]:
    """Bounding box of one field from the satellite project's polygons."""
    payload = json.loads(Path(fields_geojson).read_text(encoding="utf-8"))
    for feature in payload["features"]:
        if str(feature["properties"]["id"]) == field_id:
            ring = feature["geometry"]["coordinates"][0]
            lons = [point[0] for point in ring]
            lats = [point[1] for point in ring]
            return min(lons), min(lats), max(lons), max(lats)
    raise SystemExit(f"field {field_id!r} not found in {fields_geojson}")


def frame_image(width: int, height: int, index: int, blur: float) -> Image.Image:
    """A crop-canopy frame, optionally blurred to simulate motion smear."""
    rng = np.random.default_rng(index)
    columns = np.sin(np.linspace(0, 30 * np.pi, width) + index * 0.05) * 0.5 + 0.5
    canvas = np.zeros((height, width, 3), dtype=np.float32)
    canvas[..., 1] = 0.35 + 0.35 * columns[None, :]
    canvas[..., 0] = 0.18 + 0.10 * columns[None, :]
    canvas[..., 2] = 0.12 + 0.05 * columns[None, :]
    canvas += rng.normal(0, 0.05, canvas.shape).astype(np.float32)
    image = Image.fromarray((np.clip(canvas, 0, 1) * 255).astype(np.uint8))
    return image.filter(ImageFilter.GaussianBlur(blur)) if blur > 0 else image


def srt_block(
    index: int, start: float, end: float, lon: float, lat: float,
    rel_alt: float, abs_alt: float, when: datetime,
) -> str:
    """One SRT record in the bracketed layout DJI firmware writes."""
    def timecode(seconds: float) -> str:
        whole = int(seconds)
        millis = int(round((seconds - whole) * 1000))
        return f"{whole // 3600:02d}:{whole % 3600 // 60:02d}:{whole % 60:02d},{millis:03d}"

    return (
        f"{index}\n"
        f"{timecode(start)} --> {timecode(end)}\n"
        f'<font size="28">FrameCnt: {index}, DiffTime: 33ms\n'
        f"{when.strftime('%Y-%m-%d %H:%M:%S')}.000\n"
        f"[iso : 100] [shutter : 1/1000] [fnum : 280] [ev : 0] "
        f"[focal_len : 240] [latitude : {lat:.7f}] [longitude : {lon:.7f}] "
        f"[rel_alt: {rel_alt:.3f} abs_alt: {abs_alt:.3f}] </font>\n"
    )


def main() -> None:
    """Write a synthetic MP4 and its SRT sidecar."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("data/video/demo.mp4"))
    parser.add_argument("--field", default="rgv-002")
    parser.add_argument("--seconds", type=float, default=20.0)
    parser.add_argument("--fps", type=int, default=10, help="video frame rate")
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--rel-alt", type=float, default=60.0)
    parser.add_argument("--ground-elevation", type=float, default=12.0)
    parser.add_argument("--blur-every", type=int, default=7,
                        help="blur every Nth frame, to exercise the sharpness filter")
    parser.add_argument("--fields-geojson", type=Path,
                        default=Path(__file__).resolve().parents[2]
                        / "dosojos_sat" / "fields.geojson")
    args = parser.parse_args()

    out = args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    srt_path = out.with_suffix(".SRT")

    min_lon, min_lat, max_lon, max_lat = field_bounds(args.fields_geojson, args.field)
    n_frames = int(args.seconds * args.fps)
    start_time = datetime(2026, 9, 6, 16, 5, 0)

    import imageio_ffmpeg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    writer = subprocess.Popen(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "rgb24",
            "-s", f"{args.width}x{args.height}", "-r", str(args.fps),
            "-i", "-", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out),
        ],
        stdin=subprocess.PIPE,
    )

    blocks = []
    for index in range(n_frames):
        progress = index / max(1, n_frames - 1)
        lon = min_lon + 0.15 * (max_lon - min_lon)
        lat = min_lat + progress * (max_lat - min_lat)
        blur = 3.0 if args.blur_every and index % args.blur_every == 0 else 0.0

        image = frame_image(args.width, args.height, index, blur)
        writer.stdin.write(image.tobytes())

        start = index / args.fps
        blocks.append(
            srt_block(
                index + 1, start, start + 1 / args.fps, lon, lat,
                args.rel_alt, args.ground_elevation + args.rel_alt,
                start_time + timedelta(seconds=start),
            )
        )

    writer.stdin.close()
    if writer.wait() != 0:
        raise SystemExit("ffmpeg failed writing the video")

    srt_path.write_text("\n".join(blocks), encoding="utf-8")
    print(f"wrote {out} ({n_frames} frames at {args.fps} fps)")
    print(f"wrote {srt_path} ({len(blocks)} telemetry records)")


if __name__ == "__main__":
    main()
