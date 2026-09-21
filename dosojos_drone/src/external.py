"""Import finished maps made elsewhere, into the layout ODM would have produced.

Not every flight arrives as raw photos. A drone service, a research group or a
second software package may hand over the products instead: an orthomosaic and
elevation surfaces as GeoTIFFs, or a LiDAR point cloud. Writing them where ODM
writes its own outputs means every later step runs unchanged; 'chm' cannot tell
the difference, and the provenance file records that it should not have to.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.warp import reproject
from rasterio.windows import Window, from_bounds

from .odm_runner import EXPECTED_OUTPUTS

log = logging.getLogger(__name__)

POINT_CLOUD_SUFFIXES = (".las", ".laz")
NODATA = -9999.0

#: Ground is this low percentile of the returns in each ground cell. Not the
#: minimum, because one noise return below the surface would become the ground,
#: and not the median, because the plants standing in the cell would lift it.
DEFAULT_GROUND_PERCENTILE = 5.0

#: Ground is sampled on a coarser grid than the canopy, because plants hide the
#: soil beneath them; the coarse surface is then interpolated back onto the
#: canopy grid. Half a metre still sees every patch of soil between 0.76 m rows.
DEFAULT_GROUND_CELL_M = 0.5

#: Margin kept around the orthophoto when cropping surfaces to it, so units at
#: the edge of the image still have surface around them.
CROP_MARGIN_M = 2.0

PROVENANCE_NAME = "imported.json"


class ProductImportError(RuntimeError):
    """Raised when finished products cannot be brought into a project."""


@dataclass(frozen=True)
class Imported:
    """One product written into the project, and how it got there."""

    name: str
    source: str
    path: str
    crs: str
    shape: tuple[int, int]
    resolution_m: float
    how: str


# --------------------------------------------------------------------------- #
# CRS
# --------------------------------------------------------------------------- #


def resolve_crs(found, declared: str | None, what: str) -> CRS:
    """Pick the CRS for one input, refusing to guess.

    Raises:
        ProductImportError: if the file carries none and none was declared, or
            the declared CRS contradicts the one in the file.
    """
    declared_crs = CRS.from_user_input(declared) if declared else None
    if found is None and declared_crs is None:
        raise ProductImportError(
            f"{what} carries no coordinate system, so it cannot be placed on a map. "
            "Pass --crs with the EPSG code it was delivered in: for UTM that is "
            "326<zone> on WGS84 or 269<zone> on NAD83, e.g. EPSG:32614 for the RGV."
        )
    if found is not None and declared_crs is not None and CRS(found) != declared_crs:
        raise ProductImportError(
            f"{what} says it is in {CRS(found).to_string()} but --crs said "
            f"{declared_crs.to_string()}. Drop --crs to use the file's own."
        )
    return CRS(found) if found is not None else declared_crs


# --------------------------------------------------------------------------- #
# Rasters
# --------------------------------------------------------------------------- #


def raster_bounds(path: Path) -> tuple[tuple[float, float, float, float], CRS | None]:
    """A GeoTIFF's bounds and CRS, without reading its pixels."""
    with rasterio.open(path) as src:
        return tuple(src.bounds), src.crs


def import_raster(
    source: Path, target: Path, *, name: str, crs: str | None = None,
    bounds: tuple[float, float, float, float] | None = None,
) -> Imported:
    """Copy a GeoTIFF into place, cropped to ``bounds`` and given a CRS if it lacks one."""
    source, target = Path(source), Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(source) as src:
        final_crs = resolve_crs(src.crs, crs, source.name)
        window = Window(0, 0, src.width, src.height)
        if bounds is not None:
            wanted = from_bounds(*bounds, transform=src.transform)
            window = window.intersection(wanted.round_offsets().round_lengths())
        if window.width == src.width and window.height == src.height and src.crs:
            shutil.copyfile(source, target)
            how = "copied unchanged"
        else:
            profile = src.profile.copy()
            profile.update(
                driver="GTiff", crs=final_crs, width=int(window.width),
                height=int(window.height), transform=src.window_transform(window),
                tiled=True, blockxsize=512, blockysize=512, compress="deflate",
            )
            with rasterio.open(target, "w", **profile) as dst:
                dst.write(src.read(window=window))
            how = (f"cropped to {int(window.width)} x {int(window.height)} px"
                   if bounds is not None else "copied") + (
                       "" if src.crs else f", CRS set to {final_crs.to_string()}")
        resolution = abs(src.transform.a)
    with rasterio.open(target) as out:
        shape = (out.height, out.width)
    return Imported(name, str(source), str(target), final_crs.to_string(), shape,
                    resolution, how)


def _write_surface(target: Path, array: np.ndarray, transform, crs: CRS) -> None:
    """Write one float surface the way ODM does: float32, -9999 for nodata."""
    target.parent.mkdir(parents=True, exist_ok=True)
    data = np.where(np.isfinite(array), array, NODATA).astype(np.float32)
    with rasterio.open(
        target, "w", driver="GTiff", width=data.shape[1], height=data.shape[0],
        count=1, dtype="float32", crs=crs, transform=transform, nodata=NODATA,
        tiled=True, blockxsize=512, blockysize=512, compress="deflate", predictor=3,
    ) as dst:
        dst.write(data, 1)


# --------------------------------------------------------------------------- #
# Point clouds
# --------------------------------------------------------------------------- #


def _open_cloud(path: Path):
    try:
        import laspy
    except ImportError as exc:  # pragma: no cover - laspy is a hard dependency
        raise ProductImportError("laspy is required to read point clouds") from exc
    try:
        return laspy.open(str(path))
    except Exception as exc:  # noqa: BLE001 - any read failure is fatal here
        raise ProductImportError(f"could not read point cloud {path}: {exc}") from exc


def cloud_info(path: Path) -> dict:
    """Header facts about a point cloud: count, extent, CRS and point spacing."""
    with _open_cloud(path) as reader:
        header = reader.header
        try:
            crs = header.parse_crs()
        except Exception:  # noqa: BLE001 - a malformed CRS record is the same as none
            crs = None
        (x0, y0, _), (x1, y1, _) = header.mins, header.maxs
        n = int(header.point_count)
    area = max((x1 - x0) * (y1 - y0), 1e-9)
    return {
        "n_points": n, "bounds": (x0, y0, x1, y1), "crs": crs,
        "spacing_m": float(np.sqrt(area / max(n, 1))),
    }


def _read_points(path: Path, bounds, *, ground_only: bool = False):
    """All points inside ``bounds`` as x, y, z arrays, read in chunks."""
    xs, ys, zs = [], [], []
    with _open_cloud(path) as reader:
        for chunk in reader.chunk_iterator(5_000_000):
            x, y, z = np.asarray(chunk.x), np.asarray(chunk.y), np.asarray(chunk.z)
            keep = (x >= bounds[0]) & (x < bounds[2]) & (y > bounds[1]) & (y <= bounds[3])
            if ground_only:
                keep &= np.asarray(chunk.classification) == 2
            xs.append(x[keep])
            ys.append(y[keep])
            zs.append(z[keep])
    return np.concatenate(xs), np.concatenate(ys), np.concatenate(zs)


def _has_ground_class(path: Path) -> bool:
    """True when the cloud has points classified as ground (ASPRS class 2)."""
    with _open_cloud(path) as reader:
        for chunk in reader.chunk_iterator(2_000_000):
            if np.any(np.asarray(chunk.classification) == 2):
                return True
    return False


def grid_for(bounds, resolution_m: float):
    """Transform and shape of a north-up grid snapped to whole cells."""
    x0 = np.floor(bounds[0] / resolution_m) * resolution_m
    y1 = np.ceil(bounds[3] / resolution_m) * resolution_m
    width = int(np.ceil((bounds[2] - x0) / resolution_m))
    height = int(np.ceil((y1 - bounds[1]) / resolution_m))
    return from_origin(x0, y1, resolution_m, resolution_m), (height, width)


def _cell_index(x, y, transform, shape):
    col = np.floor((x - transform.c) / transform.a).astype(np.int64)
    row = np.floor((y - transform.f) / transform.e).astype(np.int64)
    inside = (col >= 0) & (col < shape[1]) & (row >= 0) & (row < shape[0])
    return row, col, inside


def rasterise_surface(
    source: Path, target: Path, *, crs: str | None = None,
    resolution_m: float | None = None, bounds=None,
) -> Imported:
    """A DSM from a point cloud: the highest return in each cell.

    The top of the canopy is what a surface model means, so the maximum is the
    right statistic here, where the ground surface needs a low percentile.
    """
    info = cloud_info(source)
    final_crs = resolve_crs(info["crs"], crs, Path(source).name)
    resolution = resolution_m or max(0.02, float(np.ceil(info["spacing_m"] * 100) / 100))
    cloud = info["bounds"]
    if bounds is not None:
        # Never grid beyond the scan: those cells could only ever be empty.
        bounds = (max(bounds[0], cloud[0]), max(bounds[1], cloud[1]),
                  min(bounds[2], cloud[2]), min(bounds[3], cloud[3]))
        if bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
            raise ProductImportError(
                f"{Path(source).name} does not overlap the orthophoto; check they "
                "come from the same flight and coordinate system"
            )
    else:
        bounds = cloud
    transform, shape = grid_for(bounds, resolution)

    x, y, z = _read_points(source, bounds)
    if not len(z):
        raise ProductImportError(f"{Path(source).name} has no points inside the area to import")
    row, col, inside = _cell_index(x, y, transform, shape)
    surface = np.full(shape, -np.inf)
    np.maximum.at(surface, (row[inside], col[inside]), z[inside])
    surface[~np.isfinite(surface)] = np.nan

    _write_surface(Path(target), surface, transform, final_crs)
    filled = float(np.isfinite(surface).mean())
    return Imported("dsm", str(source), str(target), final_crs.to_string(), shape,
                    resolution, f"highest of {int(inside.sum()):,} points per cell, "
                    f"{filled:.0%} of cells hit")


def rasterise_ground(
    source: Path, target: Path, *, reference_transform, reference_shape,
    crs: str | None = None, percentile: float = DEFAULT_GROUND_PERCENTILE,
    cell_m: float = DEFAULT_GROUND_CELL_M,
) -> Imported:
    """A DTM from a point cloud, on the same grid as the DSM.

    Ground-classified returns are used when the cloud has them. Otherwise the
    ground in each coarse cell is a low percentile of every return, which is
    right for a bare-soil flight or an early one where the plants are still
    small, and wrong for a closed canopy; the canopy model will show it.
    """
    info = cloud_info(source)
    final_crs = resolve_crs(info["crs"], crs, Path(source).name)
    height, width = reference_shape
    res = abs(reference_transform.a)
    bounds = (reference_transform.c, reference_transform.f - height * res,
              reference_transform.c + width * res, reference_transform.f)
    margin = 2 * cell_m
    padded = (bounds[0] - margin, bounds[1] - margin, bounds[2] + margin, bounds[3] + margin)

    ground_only = _has_ground_class(source)
    x, y, z = _read_points(source, padded, ground_only=ground_only)
    if not len(z):
        raise ProductImportError(f"{Path(source).name} has no ground points inside the area")

    coarse_transform, coarse_shape = grid_for(padded, cell_m)
    row, col, inside = _cell_index(x, y, coarse_transform, coarse_shape)
    cell = (row * coarse_shape[1] + col)[inside]
    zin = z[inside]
    order = np.lexsort((zin, cell))
    cell_sorted, z_sorted = cell[order], zin[order]
    cells, starts, counts = np.unique(cell_sorted, return_index=True, return_counts=True)
    pick = starts + np.floor((counts - 1) * percentile / 100.0).astype(np.int64)
    coarse = np.full(coarse_shape[0] * coarse_shape[1], np.nan)
    coarse[cells] = z_sorted[pick]
    coarse = coarse.reshape(coarse_shape)

    coarse = _fill_nearest(coarse)
    from scipy.ndimage import gaussian_filter

    coarse = gaussian_filter(coarse, sigma=1.0, mode="nearest")
    ground = np.full(reference_shape, np.nan)
    reproject(
        source=coarse, destination=ground,
        src_transform=coarse_transform, src_crs=final_crs,
        dst_transform=reference_transform, dst_crs=final_crs,
        resampling=Resampling.bilinear, src_nodata=np.nan, dst_nodata=np.nan,
    )
    _write_surface(Path(target), ground, reference_transform, final_crs)
    basis = "ground-classified points" if ground_only else f"p{percentile:g} of all points"
    return Imported("dtm", str(source), str(target), final_crs.to_string(),
                    tuple(reference_shape), res,
                    f"{basis} per {cell_m:g} m cell from {int(inside.sum()):,} points, "
                    "interpolated onto the DSM grid")


def _fill_nearest(array: np.ndarray) -> np.ndarray:
    """Fill NaN cells from their nearest valid neighbour."""
    from scipy.ndimage import distance_transform_edt

    holes = ~np.isfinite(array)
    if not holes.any():
        return array
    if holes.all():
        raise ProductImportError("no ground cells at all; the ground flight does not cover the area")
    _, (rows, cols) = distance_transform_edt(holes, return_indices=True)
    return array[rows, cols]


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def import_products(
    project_dir: Path, *, ortho: Path | None = None, dsm: Path | None = None,
    dtm: Path | None = None, crs: str | None = None, resolution_m: float | None = None,
    crop_to_ortho: bool = True, overwrite: bool = False,
    ground_percentile: float = DEFAULT_GROUND_PERCENTILE,
    ground_cell_m: float = DEFAULT_GROUND_CELL_M,
    source: str | None = None, lidar: bool = False,
) -> list[Imported]:
    """Bring an orthophoto and elevation surfaces into ``project_dir``.

    ``source`` records where the products came from, and ``lidar`` that the
    ground was measured by laser even though it arrives as a raster.

    Raises:
        ProductImportError: if nothing usable was given, a product already
            exists and ``overwrite`` is off, or any input cannot be placed.
    """
    project_dir = Path(project_dir)
    if dsm is None or dtm is None:
        raise ProductImportError(
            "both --dsm and --dtm are needed to measure crop height. The DTM can be "
            "a bare-ground flight, a ground raster, or the same point cloud if the "
            "soil shows between the rows."
        )
    targets = {name: project_dir / EXPECTED_OUTPUTS[name] for name in ("orthophoto", "dsm", "dtm")}
    existing = [str(p) for name, p in targets.items() if p.exists() and (name != "orthophoto" or ortho)]
    if existing and not overwrite:
        raise ProductImportError(
            "products already exist and would be overwritten: " + ", ".join(existing)
            + ". Pass --force to replace them, e.g. if they came from an earlier import."
        )

    bounds = None
    done: list[Imported] = []
    if ortho is not None:
        ortho_bounds, ortho_crs = raster_bounds(ortho)
        resolve_crs(ortho_crs, crs, Path(ortho).name)
        done.append(import_raster(ortho, targets["orthophoto"], name="orthophoto", crs=crs))
        if crop_to_ortho:
            bounds = (ortho_bounds[0] - CROP_MARGIN_M, ortho_bounds[1] - CROP_MARGIN_M,
                      ortho_bounds[2] + CROP_MARGIN_M, ortho_bounds[3] + CROP_MARGIN_M)

    if Path(dsm).suffix.lower() in POINT_CLOUD_SUFFIXES:
        surface = rasterise_surface(dsm, targets["dsm"], crs=crs,
                                    resolution_m=resolution_m, bounds=bounds)
    else:
        surface = import_raster(dsm, targets["dsm"], name="dsm", crs=crs, bounds=bounds)
    done.append(surface)

    with rasterio.open(targets["dsm"]) as ref:
        ref_transform, ref_shape = ref.transform, (ref.height, ref.width)
    if Path(dtm).suffix.lower() in POINT_CLOUD_SUFFIXES:
        done.append(rasterise_ground(
            dtm, targets["dtm"], reference_transform=ref_transform,
            reference_shape=ref_shape, crs=crs, percentile=ground_percentile,
            cell_m=ground_cell_m,
        ))
    else:
        done.append(import_raster(dtm, targets["dtm"], name="dtm", crs=crs, bounds=bounds))

    crs_seen = {item.crs for item in done}
    if len(crs_seen) > 1:
        raise ProductImportError(
            "the imported products are in different coordinate systems: "
            + ", ".join(sorted(crs_seen)) + ". Reproject them to one before importing."
        )

    (project_dir / PROVENANCE_NAME).write_text(json.dumps({
        "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": "These products were made outside this pipeline and imported, not built by ODM.",
        "source": source, "lidar": lidar,
        "products": [asdict(item) for item in done],
    }, indent=2), encoding="utf-8")
    log.info("imported %d product(s) into %s", len(done), project_dir)
    return done


def provenance(project_dir: Path) -> dict | None:
    """The import record for a project, or None if ODM built it."""
    path = Path(project_dir) / PROVENANCE_NAME
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
