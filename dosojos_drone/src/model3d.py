"""A field in 3D, small enough to turn on a phone.

OpenDroneMap turns a flight's overlapping photos into a coloured point cloud:
millions of points, each where two or more photos saw the same spot. That is
far too much for a web page, so this keeps one point per little cube of space
(the one nearest the cube's middle is as good as any), which thins the flat
ground a great deal and the plants hardly at all.

What it writes for a flight:

- ``model3d.bin``: the points, as whole numbers of ``scale`` metres from the
  middle of the field (int16 x, y, z for every point, then uint8 r, g, b for
  every point, then one uint8 per point: its height over the ground under it,
  in tenths of a metre), which a page reads straight into the graphics card;
- ``model3d.json``: how many points, the scale, where the middle is, how tall
  the plants stand over the ground, and where it came from;
- ``model3d.png``: the field seen from one corner, for a text message.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

#: About as many points as a phone turns smoothly in a web page.
TARGET_POINTS = 200_000
#: Points kept for the snapshot picture.
PICTURE_POINTS = 400_000
#: The ground under a point is the lowest ground near it: the low end of each
#: square this wide, and of the squares around it, since under a tree crown
#: the camera never saw the soil.
GROUND_CELL_M = 5.0


class ModelError(RuntimeError):
    """Raised when there is no point cloud to show, with what to do."""


@dataclass
class Model3D:
    points: int
    source_points: int
    cube_m: float
    scale_m: float
    middle: list[float]            # x, y, z of the middle, in the cloud's own CRS
    size_m: list[float]            # east-west, north-south, low-to-high
    # Heights from the middle, of the lowest ground (2nd percentile) and the highest
    # tops (99th): a sloping field puts ground in both, so this is no plant height.
    ground_m: float
    top_m: float
    # How tall the plants stand over the ground right under them (99th percentile).
    plants_top_m: float
    crs: str | None
    source: str


def read_cloud(path: Path) -> tuple[np.ndarray, np.ndarray, str | None]:
    """Points (n x 3, metres) and colours (n x 3, 0-255) of a LAS/LAZ file."""
    import laspy

    if not Path(path).exists():
        raise ModelError(f"no point cloud at {path}; run 'dosojos-drone odm' on the flight first")
    las = laspy.read(path)
    xyz = np.column_stack([np.asarray(las.x), np.asarray(las.y), np.asarray(las.z)])
    names = set(las.point_format.dimension_names)
    if {"red", "green", "blue"} <= names:
        rgb = np.column_stack([np.asarray(las.red), np.asarray(las.green),
                               np.asarray(las.blue)]).astype(np.float64)
        if rgb.max() > 255:                       # 16-bit colour
            rgb /= 256.0
        rgb = np.clip(rgb, 0, 255).astype(np.uint8)
    else:
        rgb = np.full((len(xyz), 3), 160, np.uint8)
    try:
        crs = las.header.parse_crs()
        crs_text = crs.to_string() if crs is not None else None
    except Exception:                             # a header with no CRS in it
        crs_text = None
    return xyz, rgb, crs_text


def thin(xyz: np.ndarray, target: int = TARGET_POINTS) -> tuple[np.ndarray, float]:
    """Indices of about ``target`` points, one per cube, and the cube's size in metres."""
    if len(xyz) <= target:
        return np.arange(len(xyz)), 0.0
    low = xyz.min(axis=0)
    extent = np.ptp(xyz[:, :2], axis=0)
    # Start from the cube that would give ``target`` points on a flat field and
    # grow it until few enough are left: plants add points a flat field lacks.
    cube = max(math.sqrt(float(extent[0] * extent[1]) / target), 0.01)
    for _ in range(30):
        cells = np.floor((xyz - low) / cube).astype(np.int64)
        centre = (cells + 0.5) * cube + low
        near = np.sum((xyz - centre) ** 2, axis=1)
        # Sort by cube, then by distance to its middle; the first of each cube stays.
        key = (cells[:, 0] * 73856093) ^ (cells[:, 1] * 19349663) ^ (cells[:, 2] * 83492791)
        order = np.lexsort((near, key))
        first = np.ones(len(order), bool)
        first[1:] = key[order][1:] != key[order][:-1]
        kept = order[first]
        if len(kept) <= target * 1.05:
            return np.sort(kept), cube
        cube *= math.sqrt(len(kept) / target)
    return np.sort(kept), cube


def over_ground(xyz: np.ndarray, cell_m: float = GROUND_CELL_M) -> np.ndarray:
    """Each point's height over the ground under it, in metres (0 on bare ground)."""
    from scipy.ndimage import minimum_filter

    low = xyz[:, :2].min(axis=0)
    cells = np.floor((xyz[:, :2] - low) / cell_m).astype(int)
    nx, ny = cells.max(axis=0) + 1
    flat = cells[:, 0] * ny + cells[:, 1]
    # The 5th percentile of each square, so one stray low point is not the ground.
    order = np.lexsort((xyz[:, 2], flat))
    sorted_cells = flat[order]
    starts = np.flatnonzero(np.r_[True, sorted_cells[1:] != sorted_cells[:-1]])
    counts = np.diff(np.r_[starts, len(order)])
    picks = order[starts + (counts * 0.05).astype(int)]
    grid = np.full(nx * ny, np.inf)
    grid[sorted_cells[starts]] = xyz[picks, 2]
    grid = minimum_filter(grid.reshape(nx, ny), size=3, mode="nearest").ravel()
    ground = grid[flat]
    ground[~np.isfinite(ground)] = xyz[~np.isfinite(ground), 2]
    return np.clip(xyz[:, 2] - ground, 0, None)


def build(cloud: Path, out_dir: Path, *, target: int = TARGET_POINTS,
          picture: bool = True) -> Model3D:
    """Write model3d.bin, model3d.json and (with ``picture``) model3d.png for a cloud."""
    xyz, rgb, crs = read_cloud(cloud)
    if len(xyz) < 1000:
        raise ModelError(f"{cloud.name} has only {len(xyz)} points: too few photos overlapped")
    # Stray points far above or below the field (sky, reflections) would squash
    # the view; the middle 99.8% of heights keeps every plant.
    low_z, high_z = np.percentile(xyz[:, 2], [0.1, 99.9])
    keep = (xyz[:, 2] >= low_z) & (xyz[:, 2] <= high_z)
    xyz, rgb = xyz[keep], rgb[keep]
    source_points = int(keep.sum())

    heights = over_ground(xyz)
    kept, cube = thin(xyz, target)
    pts, colours, tall = xyz[kept], rgb[kept], heights[kept]
    middle = (pts.min(axis=0) + pts.max(axis=0)) / 2
    rel = pts - middle
    scale = max(float(np.abs(rel).max()) / 32000.0, 0.001)
    ints = np.round(rel / scale).astype(np.int16)

    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "model3d.bin").open("wb") as handle:
        handle.write(ints.astype("<i2").tobytes())
        handle.write(colours.astype(np.uint8).tobytes())
        handle.write(np.clip(np.round(tall * 10), 0, 255).astype(np.uint8).tobytes())
    ground, top = np.percentile(rel[:, 2], [2, 99])
    model = Model3D(
        points=len(pts), source_points=source_points, cube_m=round(cube, 3),
        scale_m=scale, middle=[float(v) for v in middle],
        size_m=[round(float(v), 1) for v in np.ptp(pts, axis=0)],
        ground_m=round(float(ground), 2), top_m=round(float(top), 2),
        plants_top_m=round(float(np.percentile(tall, 99)), 1), crs=crs,
        source=str(cloud))
    (out_dir / "model3d.json").write_text(json.dumps(asdict(model), indent=2), "utf-8")
    if picture:
        snapshot(xyz, rgb, out_dir / "model3d.png")
    return model


def load(out_dir: Path) -> tuple[dict, bytes] | None:
    """A flight's model3d.json and model3d.bin, if it has them."""
    meta, data = out_dir / "model3d.json", out_dir / "model3d.bin"
    if not (meta.exists() and data.exists()):
        return None
    return json.loads(meta.read_text("utf-8")), data.read_bytes()


def snapshot(xyz: np.ndarray, rgb: np.ndarray, path: Path, *, width: int = 1200,
             azimuth_deg: float = 35.0, elevation_deg: float = 32.0,
             stretch: float = 2.0) -> Path:
    """The field seen from its south-west corner, heights drawn ``stretch`` times taller.

    Painted far to near, so what is closer covers what is behind it, the way an
    eye sees it. Heights are stretched because a crop two metres tall on a field
    two hundred metres long would otherwise look flat, and the picture says so.
    """
    from PIL import Image, ImageDraw, ImageFont

    if len(xyz) > PICTURE_POINTS:
        pick = np.random.default_rng(0).choice(len(xyz), PICTURE_POINTS, replace=False)
        xyz, rgb = xyz[pick], rgb[pick]
    rel = xyz - (xyz.min(axis=0) + xyz.max(axis=0)) / 2
    rel[:, 2] = (rel[:, 2] - np.percentile(rel[:, 2], 2)) * stretch
    a, e = math.radians(azimuth_deg), math.radians(elevation_deg)
    # Look from the south-west, down at the field: turn about the vertical, then tilt.
    x = rel[:, 0] * math.cos(a) - rel[:, 1] * math.sin(a)
    y = rel[:, 0] * math.sin(a) + rel[:, 1] * math.cos(a)
    screen_x = x
    screen_y = rel[:, 2] * math.cos(e) + y * math.sin(e)
    depth = y * math.cos(e) - rel[:, 2] * math.sin(e)
    span = max(np.ptp(screen_x), np.ptp(screen_y) * 1.4, 1e-6)
    s = (width - 80) / span
    height = int(np.ptp(screen_y) * s) + 120
    px = ((screen_x - screen_x.min()) * s + 40).astype(int)
    py = (height - 60 - (screen_y - screen_y.min()) * s).astype(int)
    canvas = np.full((height, width, 3), (243, 245, 241), np.uint8)
    # Dots as wide as the gap between neighbouring points, so the ground looks solid.
    area = max(float(np.ptp(rel[:, 0]) * np.ptp(rel[:, 1])), 1.0)
    dot = int(min(6, max(2, math.ceil(s / math.sqrt(len(rel) / area)))))
    order = np.argsort(-depth)                  # far first
    for dx in range(dot):
        for dy in range(dot):
            xs, ys = np.clip(px[order] + dx, 0, width - 1), np.clip(py[order] + dy, 0, height - 1)
            canvas[ys, xs] = rgb[order]
    image = Image.fromarray(canvas)
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("arial.ttf", 22)
    except OSError:
        font = ImageFont.load_default()
    draw.text((20, 14), f"3D model from the drone photos (heights drawn {stretch:g}x taller)",
              font=font, fill=(29, 42, 34))
    image.save(path, optimize=True)
    return path
